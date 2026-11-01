"""MapleStory Classic experience/meso rate calculations and local SQLite storage."""
from __future__ import annotations

import csv
import bisect
import math
import sqlite3
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Confirmed classic-world requirements for Lv 1..49; later entries historical
# estimates, not game-verified. See README for source and limitations.
EXP_REQUIREMENTS = [
    0, 15, 34, 57, 92, 135, 372, 560, 840, 1242, 1716, 2360, 3216,
    4200, 5460, 7050, 8840, 11040, 13716, 16680, 20216, 24402,
    28980, 34320, 40512, 47216, 54900, 63666, 73080, 83720, 95700,
    108480, 122760, 138666, 155540, 174216, 194832, 216600, 240500,
    266682, 294216, 324240, 356916, 391160, 428280, 468450, 510420,
    555680, 604416, 655200, 709716, 748608, 789631, 832902, 878545,
    926689, 977492, 1031065, 1087569, 1147192, 1209990,
]

# A balance change observed a few seconds after the first MESOS reading
# cannot give a useful 1-hour hunting projection. Wait for a meaningful
# observation window, and never treat a closed-inventory gap as an instant
# pickup when the inventory reappears.
MIN_MESO_FORECAST_SECONDS = 30.0


def requirement(level: int, observed: int | None = None) -> int | None:
    if level <= 0:
        return None
    if level < len(EXP_REQUIREMENTS):
        return EXP_REQUIREMENTS[level]
    return observed


def accumulated(level: int, exp: int) -> int | None:
    if level <= 0 or level > len(EXP_REQUIREMENTS):
        return None
    return sum(EXP_REQUIREMENTS[1:level]) + exp


def fmt_int(value: int | float | None, signed=False) -> str:
    if value is None:
        return "—"
    return f"{round(value):+,.0f}" if signed else f"{round(value):,.0f}"


def fmt_hms(seconds: float) -> str:
    value = max(0, int(seconds))
    hh, remainder = divmod(value, 3600)
    mm, ss = divmod(remainder, 60)
    return f"{hh:02}:{mm:02}:{ss:02}"


def eta_text(seconds: float | None) -> str:
    if seconds is None or not math.isfinite(seconds):
        return "데이터를 모으는 중"
    if seconds < 60:
        return f"약 {max(1, round(seconds))}초"
    hours, minutes = divmod(round(seconds / 60), 60)
    return f"약 {hours}시간 {minutes}분" if hours else f"약 {minutes}분"


@dataclass
class Sample:
    elapsed: float
    level: int | None
    exp: int | None
    total_exp: int | None
    meso: int | None
    when: str


class HuntSession:
    def __init__(self, level: int, map_name: str):
        self.level = level
        self.map_name = map_name.strip() or "미지정 사냥터"
        self.started = datetime.now().astimezone()
        self.start_clock = time.monotonic()
        self.paused_at: float | None = None
        self.paused_total = 0.0
        self.last_sample_at = 0.0
        self.last_exp_at: float | None = None
        # Around 2 EXP reads/sec is supported. Keep more than an hour of
        # samples for the 60-minute rate/graph without unbounded allocations.
        self.samples: deque[Sample] = deque(maxlen=10000)
        self.initial_exp: int | None = None
        self.initial_meso: int | None = None
        self.meso_base_elapsed: float | None = None
        self.meso_last_seen_elapsed: float | None = None
        # Actual inventory sightings only (not EXP samples that repeat the
        # last known MESOS balance). The chart plots cumulative-average rates
        # derived from these points, never instant pickup spikes.
        self.meso_sightings: deque[tuple[float, int]] = deque(maxlen=5000)
        self.current_exp: int | None = None
        self.current_pct: float | None = None
        self.current_meso: int | None = None
        self.total_exp_base: int | None = None
        self.latest_total_exp: int | None = None
        self.notes: list[tuple[float, str, int]] = []
        self.db_id: int | None = None
        self.stopped = False
        self.ocr_status = "게임 화면 감지 중"

    def elapsed(self) -> float:
        ref = self.paused_at if self.paused_at is not None else time.monotonic()
        return max(0.0, ref - self.start_clock - self.paused_total)

    @property
    def paused(self) -> bool:
        return self.paused_at is not None

    def pause(self) -> None:
        if self.paused_at is None:
            self.paused_at = time.monotonic()

    def resume(self) -> None:
        if self.paused_at is not None:
            self.paused_total += time.monotonic() - self.paused_at
            self.paused_at = None

    @property
    def gained_exp(self) -> int:
        if self.total_exp_base is None or self.latest_total_exp is None:
            return 0
        return self.latest_total_exp - self.total_exp_base

    @property
    def gained_meso(self) -> int:
        if self.initial_meso is None or self.current_meso is None:
            return 0
        return self.current_meso - self.initial_meso

    @property
    def meso_observed_seconds(self) -> float | None:
        """Active hunting time covered by actual balance observations."""
        if self.initial_meso is None or self.current_meso is None:
            return None
        if self.meso_base_elapsed is None or self.meso_last_seen_elapsed is None:
            return None
        return max(0.0, self.meso_last_seen_elapsed - self.meso_base_elapsed)

    def meso_session_rate(self) -> float | None:
        """Meso/hour based on baseline-to-latest confirmed balance.

        No subtraction between adjacent samples. An inventory may be closed
        for 20 minutes; those 20 minutes count in the denominator at the next
        reading, avoiding a false burst when the inventory is reopened.
        While closed, the rate stays on the last confirmed observation
        instead of falling toward zero with an unchanged cached balance.
        Paused time is excluded by elapsed().
        """
        observed = self.meso_observed_seconds
        if observed is None or observed < MIN_MESO_FORECAST_SECONDS:
            return None
        return self.gained_meso * 3600.0 / observed

    def meso_average_chart(self, minutes: int, buckets: int = 12) -> list[float]:
        """Meso/min trend of *cumulative averages*, not instantaneous pickups.

        Interpolate rates between actual inventory observations and carry the
        last estimate while the inventory is closed. This is intentionally
        a smoothed projection, NOT a precise per-minute loot history.
        """
        if buckets < 1:
            return []
        if self.meso_session_rate() is None or self.meso_base_elapsed is None:
            return [0.0] * buckets
        base = self.meso_base_elapsed
        readings: list[tuple[float, float]] = [
            (stamp, (balance - self.initial_meso) * 60.0 / (stamp - base))
            for stamp, balance in self.meso_sightings
            if stamp - base >= MIN_MESO_FORECAST_SECONDS
        ]
        if not readings:
            return [0.0] * buckets
        # Only the latest observed times are meaningful; do not shift the
        # whole graph forward every second while inventory is hidden.
        end = readings[-1][0]
        beginning = max(base, end - max(1, minutes) * 60)
        times = [stamp for stamp, _ in readings]
        trend = []
        for i in range(buckets):
            at = beginning + (end - beginning) * (i + 1) / buckets
            index = bisect.bisect_right(times, at)
            if index == 0:
                rate = readings[0][1]
            elif index == len(readings):
                rate = readings[-1][1]
            else:
                t0, r0 = readings[index - 1]
                t1, r1 = readings[index]
                proportion = max(0.0, min(1.0, (at - t0) / max(1e-8, t1 - t0)))
                rate = r0 + (r1 - r0) * proportion
            trend.append(rate)
        return trend

    def add_exp(self, level: int, exp: int, pct: float | None) -> bool:
        if self.stopped or self.paused or not (1 <= level <= 200) or exp < 0:
            return False
        total = accumulated(level, exp)
        if total is None:
            # Exact level-up accounting needs an EXP table. Do not fabricate it.
            self.level, self.current_exp, self.current_pct = level, exp, pct
            return False
        limit = requirement(level)
        if limit and exp >= limit:
            return False
        # The game's OCR occasionally emits "0 (0%)" despite nonzero EXP.
        # v1.9.4 accepted that sample as genuine same-level EXP loss, making
        # the live total and the 6/10/30/60-minute timers negative. A new
        # session must also never anchor on this false zero and then mistake
        # the next good 12,000+ EXP reading for a huge hunting gain. The
        # first real positive EXP reading provides a trustworthy baseline.
        if exp == 0 and (self.current_exp is None or level == self.level):
            return False
        if self.latest_total_exp is not None:
            delta = total - self.latest_total_exp
            # Level drop or OCR outliers: require explicit new session.
            if level < self.level:
                return False
            # Same-level EXP is monotonic during normal hunting. Even minor
            # negative OCR jumps (<500 EXP) corrupted rolling-rate readings.
            # A real death penalty is not automatically counted: use the
            # visible "새 세션" control to re-anchor after losing EXP.
            if level == self.level and delta < 0:
                return False
            if level == self.level and self.last_exp_at is not None:
                elapsed_since = max(0.0, self.elapsed() - self.last_exp_at)
                # A single OCR sample must not produce over 100K EXP in a
                # few seconds. Allow a generous catch-up rate when the game
                # was temporarily hidden/uncapturable, but reject wild jumps.
                if delta > max(2500, 750 * elapsed_since):
                    return False
            # One OCR reading cannot plausibly supply >1.5 full levels.
            if delta > max(25000, int((limit or 10000) * 1.5)):
                return False
        if self.total_exp_base is None:
            self.total_exp_base = total
            self.initial_exp = exp
        self.level, self.current_exp, self.current_pct = level, exp, pct
        self.latest_total_exp = total
        self.last_exp_at = self.elapsed()
        self._sample()
        return True

    def add_meso(self, meso: int) -> bool:
        if self.stopped or self.paused or not 0 <= meso <= 9_999_999_999:
            return False
        elapsed = self.elapsed()
        first = self.initial_meso is None
        changed = meso != self.current_meso
        if self.initial_meso is None:
            self.initial_meso = meso
            self.meso_base_elapsed = elapsed
        self.current_meso = meso
        self.meso_last_seen_elapsed = elapsed
        # The same amount seen again also confirms that no increase happened
        # during the observed interval. Keep a compact timeline for average
        # trends without collecting one point per frame indefinitely.
        if (not self.meso_sightings or changed
            or elapsed - self.meso_sightings[-1][0] >= 10):
            self.meso_sightings.append((elapsed, meso))
        # Store the changed balance in the existing EXP sample timeline for
        # history/CSV compatibility. Repeated sightings aren't gain events.
        if first or changed:
            self._sample(force=True)
        return True

    def _sample(self, force: bool = False):
        elapsed = self.elapsed()
        # The numeric HUD updates every ~0.5s, while trend charts need at
        # most about one sample/0.6s. Live EXP is updated before this check.
        if not force and self.samples and elapsed - self.last_sample_at < 0.6:
            return
        self.samples.append(Sample(elapsed, self.level, self.current_exp,
                                   self.latest_total_exp, self.current_meso,
                                   datetime.now().astimezone().isoformat(timespec="seconds")))
        self.last_sample_at = elapsed

    def rolling_rate(self, kind: str, minutes: int) -> float | None:
        if kind == "meso":
            # Keep the old public calculation API safe for any callers.
            # MESOS is only observed while inventory is visible; EXP samples
            # must never masquerade as MESOS observations.
            return self.meso_session_rate()
        series = [(x.elapsed, x.total_exp if kind == "exp" else x.meso)
                  for x in self.samples]
        series = [(t, value) for t, value in series if value is not None]
        if len(series) < 2:
            return None
        end_t, end_v = series[-1]
        threshold = end_t - minutes * 60
        before = [item for item in series if item[0] <= threshold]
        start_t, start_v = before[-1] if before else series[0]
        dt = end_t - start_t
        if dt < 10:
            return None
        return (end_v - start_v) * 3600 / dt

    def until_level(self, rate: float | None) -> float | None:
        req = requirement(self.level)
        if req is None or self.current_exp is None or not rate or rate <= 0:
            return None
        return max(0, (req - self.current_exp) / rate * 3600)

    def chart(self, kind: str, minutes: int, buckets: int = 12) -> list[float]:
        if kind == "meso":
            return self.meso_average_chart(minutes, buckets)
        pairs = [(x.elapsed, x.total_exp if kind == "exp" else x.meso)
                 for x in self.samples]
        pairs = [(t, v) for t, v in pairs if v is not None]
        if len(pairs) < 2:
            return [0.0] * buckets
        end_t = pairs[-1][0]
        first = max(pairs[0][0], end_t - minutes * 60)
        width = max(1, end_t - first) / buckets
        values: list[float] = []
        for i in range(buckets):
            a = first + i * width
            b = first + (i + 1) * width
            relevant = [(t,v) for t,v in pairs if a <= t <= b]
            if len(relevant) >= 2:
                values.append((relevant[-1][1] - relevant[0][1]) / max(1, relevant[-1][0] - relevant[0][0]) * 60)
            else:
                left = [(t,v) for t,v in pairs if t <= a]
                right = [(t,v) for t,v in pairs if t <= b]
                if left and right and right[-1][0] > left[-1][0]:
                    values.append((right[-1][1] - left[-1][1]) * 60 / max(1, right[-1][0] - left[-1][0]))
                else:
                    values.append(0)
        return values


class SessionStore:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self.db = sqlite3.connect(directory / "hunting.sqlite3")
        self.db.execute("""CREATE TABLE IF NOT EXISTS sessions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, started TEXT NOT NULL,
            ended TEXT, map TEXT, level_start INTEGER, level_end INTEGER,
            duration INTEGER DEFAULT 0, gained_exp INTEGER DEFAULT 0,
            gained_meso INTEGER DEFAULT 0, meso_initial INTEGER,
            meso_final INTEGER, notes TEXT DEFAULT ''
        )""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS samples(
            session_id INTEGER NOT NULL, recorded TEXT NOT NULL,
            elapsed REAL NOT NULL, level INTEGER, exp INTEGER,
            total_exp INTEGER, meso INTEGER
        )""")
        self.db.commit()

    def begin(self, hunt: HuntSession):
        cursor = self.db.execute(
            "INSERT INTO sessions(started,map,level_start) VALUES (?,?,?)",
            (hunt.started.isoformat(timespec="seconds"), hunt.map_name, hunt.level))
        hunt.db_id = cursor.lastrowid
        self.db.commit()

    def save(self, hunt: HuntSession):
        if hunt.db_id is None:
            return
        self.db.execute(
            """UPDATE sessions SET level_end=?,duration=?,gained_exp=?,gained_meso=?,
             meso_initial=?,meso_final=?,notes=? WHERE id=?""",
            (hunt.level, int(hunt.elapsed()), hunt.gained_exp, hunt.gained_meso,
             hunt.initial_meso, hunt.current_meso,
             "; ".join(f"{note}: {value:,}" for _, note, value in hunt.notes), hunt.db_id))
        # Records at most one sample per 20 seconds in the database, keeping
        # fine-grained rolling data only in memory to avoid noisy disk writes.
        row = self.db.execute(
            "SELECT MAX(elapsed) FROM samples WHERE session_id=?", (hunt.db_id,)).fetchone()
        previous = row[0] if row and row[0] is not None else -999
        if hunt.samples and (hunt.samples[-1].elapsed - previous >= 20):
            s = hunt.samples[-1]
            self.db.execute("""INSERT INTO samples
                (session_id,recorded,elapsed,level,exp,total_exp,meso)
                VALUES(?,?,?,?,?,?,?)""",
                (hunt.db_id, s.when, s.elapsed, s.level, s.exp, s.total_exp, s.meso))
        self.db.commit()

    def finish(self, hunt: HuntSession):
        hunt.pause()
        hunt.stopped = True
        self.save(hunt)
        self.db.execute("UPDATE sessions SET ended=? WHERE id=?", (
            datetime.now().astimezone().isoformat(timespec="seconds"), hunt.db_id))
        self.db.commit()

    def recent(self, count=40):
        self.db.row_factory = sqlite3.Row
        return self.db.execute("SELECT * FROM sessions ORDER BY id DESC LIMIT ?", (count,)).fetchall()

    def export(self, path: Path) -> int:
        columns = ("id", "started", "ended", "map", "level_start", "level_end",
                   "duration", "gained_exp", "gained_meso", "meso_initial", "meso_final", "notes")
        data = self.db.execute("SELECT " + ",".join(columns) + " FROM sessions ORDER BY id DESC").fetchall()
        with path.open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.writer(file)
            writer.writerow(columns)
            writer.writerows(data)
        return len(data)

    def close(self) -> None:
        self.db.commit()
        self.db.close()
