"""Independent, one-shot, EXP accumulation timers.

Each timer begins with the first valid game EXP reading of a new hunt
session. Results freeze at the timer cutoff, not at the latest rolling
window. Paused session time is excluded by the caller's elapsed clock.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# Respect the user's four distinct requested durations, including 6 minutes.
TIMERS = ((10, 600), (30, 1800), (6, 360), (60, 3600))
LABELS = {10: "10분", 30: "30분", 6: "6분", 60: "1시간"}
SCAN_GAP_LIMIT_SECONDS = 12.0


@dataclass(frozen=True)
class TimerResult:
    minutes: int
    target_seconds: int
    exp: int | None
    sample_gap: float
    status: str  # complete | missing_scan

    def as_dict(self) -> dict[str, Any]:
        return {
            "minutes": self.minutes,
            "target_seconds": self.target_seconds,
            "exp": self.exp,
            "sample_gap": round(self.sample_gap, 3),
            "status": self.status,
        }


class ExpTimerSet:
    def __init__(self):
        self.anchor_elapsed: float | None = None
        self.anchor_total: int | None = None
        self.readings: list[tuple[float, int]] = []
        self.results: dict[int, TimerResult] = {}

    def observe(self, *, elapsed: float, total_exp: int) -> list[TimerResult]:
        """Store a validated OCR sample and freeze timers crossing cutoff.

        The last actual pre-cutoff reading is used, never a late reading. If
        OCR was absent for 12+ seconds before cutoff, mark as unavailable
        instead of incorrectly claiming an exact accumulated amount.
        """
        if self.anchor_elapsed is None:
            self.anchor_elapsed = elapsed
            self.anchor_total = total_exp
            self.readings = [(0.0, total_exp)]
            return []
        now = max(0.0, elapsed - self.anchor_elapsed)
        if self.readings and now < self.readings[-1][0]:
            return []
        self.readings.append((now, total_exp))
        newly_done = []
        for minutes, target in TIMERS:
            if minutes in self.results or now < target:
                continue
            previous = next(((at, exp) for at, exp in reversed(self.readings)
                             if at <= target), None)
            gap = target - previous[0] if previous is not None else target
            reliable = previous is not None and gap <= SCAN_GAP_LIMIT_SECONDS
            result = TimerResult(minutes=minutes, target_seconds=target,
                                 exp=(previous[1] - self.anchor_total if reliable else None),
                                 sample_gap=gap,
                                 status="complete" if reliable else "missing_scan")
            self.results[minutes] = result
            newly_done.append(result)
        return newly_done

    def passed(self, elapsed: float) -> float | None:
        if self.anchor_elapsed is None:
            return None
        return max(0.0, elapsed - self.anchor_elapsed)

    def running_gain(self) -> int | None:
        if self.anchor_total is None or not self.readings:
            return None
        return self.readings[-1][1] - self.anchor_total

    def display(self, minutes: int, elapsed: float) -> tuple[str, str, str]:
        """(EXP text, remaining-time/status text, state)."""
        result = self.results.get(minutes)
        if result is not None:
            if result.status == "complete":
                return f"{result.exp:+,} EXP", "완료", "complete"
            return "기록 불가", "스캔 누락", "missing_scan"
        passed = self.passed(elapsed)
        if passed is None:
            return "인식 대기", "시작 대기", "waiting"
        duration = minutes * 60
        if passed >= duration:
            return "결과 확인 중", "OCR 대기", "verifying"
        gain = self.running_gain()
        remaining = max(0, int(duration - passed + 0.999))
        clock = f"{remaining // 60:02}:{remaining % 60:02}"
        return (f"{gain:+,} EXP" if gain is not None else "인식 대기",
                clock, "running")

    def summary(self, elapsed: float, session_id: int | None = None) -> dict:
        return {
            "session_id": session_id,
            "elapsed_seconds": round(elapsed, 2),
            "timer_started": self.anchor_elapsed is not None,
            "running_exp": self.running_gain(),
            "timers": {LABELS[minutes]: self.results[minutes].as_dict()
                       if minutes in self.results else None
                       for minutes, _ in TIMERS},
        }
