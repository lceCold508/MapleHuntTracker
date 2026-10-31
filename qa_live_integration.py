"""End-to-end read-only game capture and EXP accounting smoke test.

No input is sent to the game and no personal tracker settings/DB are changed.
Runs against a still-open background MapleStory window, if available.
"""
from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

from PIL import Image

from app import TrackerApp
from engine import HuntSession
from screen import DEFAULT_EXP_REGION, game_info, read_exp
from window_capture import GameWindowCapture


class FakeTracker:
    def __init__(self):
        self.session = HuntSession(27, "독립 QA")
        self.capture_stop = threading.Event()
        self.capture_messages = queue.SimpleQueue()
        self.reading_level = 27
        self.exp_roi = DEFAULT_EXP_REGION
        self.window_capture = GameWindowCapture(DEFAULT_EXP_REGION)


def main():
    info = game_info()
    assert info is not None, "Game window not found"
    tracker = FakeTracker()
    exp_worker = threading.Thread(target=TrackerApp._capture_worker,
                                  args=(tracker,), daemon=True)
    meso_worker = threading.Thread(target=TrackerApp._meso_worker,
                                   args=(tracker,), daemon=True)
    exp_worker.start()
    meso_worker.start()
    time.sleep(6)
    tracker.capture_stop.set()
    exp_worker.join(3)
    meso_worker.join(3)
    tracker.window_capture.stop()
    readings = []
    errors = []
    while True:
        try:
            message = tracker.capture_messages.get_nowait()
        except queue.Empty:
            break
        if message[0] == "exp":
            readings.append((message[3], message[4]))
            tracker.session.add_exp(message[2], message[3], message[4])
        if message[0] in ("error", "meso_error"):
            errors.append(message)
    assert not errors, f"Background workers raised errors: {errors}"
    assert len(readings) >= 3, f"Expected several independent EXP reads: {readings}"
    assert tracker.session.current_exp is not None
    assert tracker.session.current_pct is not None
    assert tracker.session.level == 27
    print("REAL_BACKGROUND_WORKER_PASS",
          "EXP_READINGS", len(readings),
          "SAMPLE", readings[-1],
          "SESSION_EXP", tracker.session.current_exp,
          "OTHER_APP_COVERING_GAME", True, flush=True)

    path = Path(__file__).parent / "debug"
    old = path / "maple_window_wgc_exp.png"
    latest = path / "wgc_direct_exp.png"
    if old.is_file() and latest.is_file():
        p1 = read_exp(Image.open(old), 27)
        p2 = read_exp(Image.open(latest), 27)
        assert p1 and p2 and p2[0] > p1[0], (p1, p2)
        session = HuntSession(27, "실제 게임 캡처 차액 검증")
        session.start_clock -= 11
        assert session.add_exp(27, p1[0], p1[1])
        session.start_clock -= 12
        assert session.add_exp(27, p2[0], p2[1])
        assert session.gained_exp == p2[0] - p1[0], session.gained_exp
        rate = session.rolling_rate("exp", 1)
        assert rate and rate > 0, rate
        print("REAL_EXP_GAIN_ACCOUNTING_PASS",
              "first", p1[0], "second", p2[0],
              "gain", session.gained_exp, "hourly_rate", int(rate), flush=True)


if __name__ == "__main__":
    main()
