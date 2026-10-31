"""EXP 0.5-second cadence and independent MESO worker regression tests."""
from __future__ import annotations

import queue
import threading
import time
from unittest.mock import patch

from PIL import Image

import app
import screen
from engine import HuntSession


def test_fast_ocr_one_pass():
    # A valid EXP/percentage pair should require just one OCR pass.
    image = Image.new("RGB", (250, 35), "black")
    reads = []

    def recognize(_):
        reads.append(1)
        return "EXP 34,950[54.89%]"

    with patch.object(screen, "recognize", side_effect=recognize):
        result = screen.read_exp(image, 27)
    assert result and result[:2] == (34950, 54.89), result
    assert len(reads) == 1, f"Fast EXP OCR should use 1 pass, got {len(reads)}"


def test_rapid_exp_samples_bound_memory():
    session = HuntSession(27, "테스트")
    session.start_clock -= 20
    assert session.add_exp(27, 1000, 1.57)
    count = len(session.samples)
    assert session.add_exp(27, 1001, 1.57)
    # Raw EXP updates even when a chart sample is throttled.
    assert session.current_exp == 1001 and len(session.samples) == count
    session.start_clock -= 1
    assert session.add_exp(27, 1040, 1.63)
    assert len(session.samples) == count + 1
    # A MESO change still appears immediately in the graph.
    assert session.add_meso(12000)
    assert len(session.samples) == count + 2


def test_exp_not_blocked_by_slow_fullscreen_meso():
    class FakeTracker:
        def __init__(self):
            self.session = HuntSession(27, "가짜 화면")
            self.capture_stop = threading.Event()
            self.capture_messages = queue.SimpleQueue()
            self.reading_level = 27
            self.exp_roi = [0.5, 0.92, 0.7, 0.99]

    class SlowMeso:
        def reset(self):
            return

        def detect(self, _):
            time.sleep(1.45)
            return None

    fake = FakeTracker()
    moments = []
    tiny = Image.new("RGB", (120, 30))

    def fake_exp(image, level, previous):
        assert image is tiny and level == 27
        moments.append(time.monotonic())
        time.sleep(0.035)
        value = 34000 + 15 * len(moments)
        return value, value / 63666 * 100, "test"

    with (patch.object(app, "game_info", return_value=object()),
          patch.object(app, "capture_region", return_value=tiny),
          patch.object(app, "read_exp", side_effect=fake_exp),
          patch.object(app, "grab_game", return_value=tiny),
          patch.object(app, "AutoMesoDetector", SlowMeso)):
        exp_thread = threading.Thread(target=app.TrackerApp._capture_worker,
                                      args=(fake,), daemon=True)
        meso_thread = threading.Thread(target=app.TrackerApp._meso_worker,
                                       args=(fake,), daemon=True)
        exp_thread.start()
        meso_thread.start()
        time.sleep(2.55)
        fake.capture_stop.set()
        exp_thread.join(2)
        meso_thread.join(2)
    assert not exp_thread.is_alive() and not meso_thread.is_alive()
    assert len(moments) >= 5, f"EXP only updated {len(moments)} times in 2.55s"
    gaps = [b - a for a, b in zip(moments, moments[1:])]
    assert gaps and max(gaps) < 0.90, f"Slow MESO blocked EXP: {gaps}"
    print("FAST_EXP_INDEPENDENT_WORKERS_PASS",
          "samples=", len(moments),
          "max_gap_s=", round(max(gaps), 3),
          "target_s=", app.EXP_POLL_INTERVAL,
          "ui_tick_ms=", app.UI_TICK_MS)


def test_stale_level_recovers_without_manual_typing():
    # A friend may start at Lv 27 while tracker saved Lv 26 yesterday.
    # Two consistent EXP snapshots at Lv 27 should recalibrate automatically.
    class FakeTracker:
        def __init__(self):
            self.session = HuntSession(26, "오래된 설정")
            self.capture_stop = threading.Event()
            self.capture_messages = queue.SimpleQueue()
            self.reading_level = 26
            self.exp_roi = [0.5, 0.92, 0.7, 0.99]

    fake = FakeTracker()
    tiny = Image.new("RGB", (120, 30))

    def read_for_level(image, level, previous=None):
        if level == 27:
            return 2715, 4.26, "EXP 2,715[4.26%]"
        return None

    with (patch.object(app, "game_info", return_value=object()),
          patch.object(app, "capture_region", return_value=tiny),
          patch.object(app, "read_exp", side_effect=read_for_level)):
        thread = threading.Thread(target=app.TrackerApp._capture_worker,
                                  args=(fake,), daemon=True)
        thread.start()
        time.sleep(1.22)
        fake.capture_stop.set()
        thread.join(2)
    messages = []
    while True:
        try:
            messages.append(fake.capture_messages.get_nowait())
        except queue.Empty:
            break
    assert any(m[0] == "exp" and m[2] == 27 and m[3] == 2715 for m in messages), (
        "Expected two matching observations to recalibrate stale level: "
        + str(messages))
    print("STALE_EXP_LEVEL_AUTOCALIBRATION_PASS from=26 to=27")


if __name__ == "__main__":
    test_fast_ocr_one_pass()
    test_rapid_exp_samples_bound_memory()
    test_exp_not_blocked_by_slow_fullscreen_meso()
    test_stale_level_recovers_without_manual_typing()
