"""Regression tests for v1.9.5 EXP OCR corruption.

Actual v1.9.4 user records contained Lv.32 11,359 EXP -> 0 EXP -> 12,870
EXP, causing negative hunting gain followed by an impossible ~12K gain.  OCR
errors must not propagate into the baseline, rolling rate or timed counters.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from engine import HuntSession, SessionStore, requirement
from overlay_app import exp_rejection_reason
from timed_exp import ExpTimerSet


def check_false_zero():
    hunt = HuntSession(32, "이블아이굴")
    timers = ExpTimerSet()
    assert not hunt.add_exp(32, 0, 0.0)
    assert hunt.current_exp is None
    assert hunt.initial_exp is None
    assert hunt.total_exp_base is None
    assert exp_rejection_reason(hunt, 32, 0) is not None

    assert hunt.add_exp(32, 11359, round(11359 / requirement(32) * 100, 2))
    first_total = hunt.latest_total_exp
    timers.observe(elapsed=0, total_exp=first_total)
    hunt.start_clock -= 12
    assert hunt.add_exp(32, 11396, round(11396 / requirement(32) * 100, 2))
    timers.observe(elapsed=12, total_exp=hunt.latest_total_exp)
    rate_before = hunt.rolling_rate("exp", 1)
    assert rate_before is not None and rate_before > 0
    assert hunt.gained_exp == 37

    # Reproduce the specific observed 11,396 -> 0 OCR bug.
    for bad in (0, 10367, 11300, 0, 11100):
        assert exp_rejection_reason(hunt, 32, bad) is not None
        assert not hunt.add_exp(32, bad, 0 if bad == 0 else None)
        assert hunt.current_exp == 11396
        assert hunt.latest_total_exp == first_total + 37
        assert hunt.gained_exp == 37
        assert hunt.rolling_rate("exp", 1) == rate_before
    assert hunt.add_exp(32, 11432, round(11432 / requirement(32) * 100, 2))
    timers.observe(elapsed=16, total_exp=hunt.latest_total_exp)
    assert hunt.gained_exp == 73
    assert timers.running_gain() == 73

    # Real level-up is not mistaken for same-level OCR regression.
    assert exp_rejection_reason(hunt, 33, 0) is None
    assert hunt.add_exp(33, 0, 0.0)
    assert hunt.gained_exp == requirement(32) - 11359
    assert hunt.gained_exp > 0
    # Replayed same-level EXP samples from the actual corrupted session:
    # old builds accepted the 10,855 -> 10,367 drop and subsequent 0.
    recorded = HuntSession(32, "replay")
    first = 10056
    assert recorded.add_exp(32, first, 8.19)
    observed = (10630, 10720, 10855, 10367, 11097, 11359, 0, 12702, 12870)
    for item in observed:
        accepted = recorded.add_exp(32, item, round(item / requirement(32) * 100, 2))
        if item == 10367 or item == 0:
            assert not accepted, item
    assert recorded.current_exp == 12870
    assert recorded.gained_exp == 2814
    assert all(s.total_exp >= recorded.total_exp_base for s in recorded.samples)
    print("EXP_FALSE_ZERO_AND_MONOTONICITY_PASS")


def check_persistence():
    with tempfile.TemporaryDirectory(prefix="maple-exp-integrity-") as home:
        db = SessionStore(Path(home))
        hunt = HuntSession(32, "exp-test")
        db.begin(hunt)
        assert hunt.add_exp(32, 12870, 10.48)
        hunt.start_clock -= 11
        assert hunt.add_exp(32, 12907, 10.51)
        for incorrect in (0, 10000, 0):
            assert not hunt.add_exp(32, incorrect, 0)
        hunt.start_clock -= 11
        assert hunt.add_exp(32, 12944, 10.54)
        assert hunt.gained_exp == 74
        assert hunt.rolling_rate("exp", 1) > 0
        db.finish(hunt)
        row = db.db.execute("SELECT gained_exp FROM sessions WHERE id=?",
                            (hunt.db_id,)).fetchone()
        assert row == (74,), row
        db.close()
    print("EXP_SESSION_AND_DATABASE_PASS")


if __name__ == "__main__":
    check_false_zero()
    check_persistence()
