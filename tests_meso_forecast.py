"""Regression: inventory open/close must never create phantom MESOS/min spikes."""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import app
from engine import HuntSession, MIN_MESO_FORECAST_SECONDS


def near(actual, expected, epsilon=0.00001):
    assert actual is not None and abs(actual - expected) <= epsilon, (
        f"Expected {expected}, got {actual}")


def test_long_closed_inventory_reopening():
    """A 15-minute inventory gap is not a 1-second pickup burst."""
    with patch("engine.time.monotonic") as clock:
        clock.return_value = 1000
        hunt = HuntSession(27, "개미굴")
        # The first balance is unknown until the inventory is first opened:
        # the game had already been running for five minutes.
        clock.return_value = 1300
        assert hunt.add_meso(10_000)
        assert hunt.initial_meso == 10_000
        assert hunt.meso_base_elapsed == 300
        assert hunt.meso_session_rate() is None

        clock.return_value = 1360
        assert hunt.add_meso(10_600)
        near(hunt.meso_observed_seconds, 60)
        near(hunt.meso_session_rate(), 36_000)  # 600 per min
        assert hunt.rolling_rate("meso", 1) == hunt.meso_session_rate()
        before_close = hunt.meso_session_rate()
        readings = len(hunt.meso_sightings)

        # Fifteen minutes of hunting while the inventory is CLOSED.
        # It must not interpolate a new amount or shrink the last confirmed
        # projection simply because the game is still running.
        clock.return_value = 2260
        near(hunt.meso_session_rate(), before_close)
        near(hunt.meso_observed_seconds, 60)
        assert len(hunt.meso_sightings) == readings

        # Reopen: total is +6,600 over 16 minutes of measured playtime,
        # not +6,000 in one second since the previous HUD update.
        clock.return_value = 2261
        assert hunt.add_meso(16_600)
        near(hunt.meso_observed_seconds, 961)
        expected = 6600 * 3600 / 961
        near(hunt.meso_session_rate(), expected)
        assert abs(hunt.meso_session_rate()) < 30_000
        assert hunt.initial_meso == 10_000
        assert hunt.current_meso == 16_600
        assert hunt.gained_meso == 6_600
        assert hunt.rolling_rate("meso", 1) == hunt.rolling_rate("meso", 60)
        for period in (1, 5, 10, 15, 30, 60):
            bars = hunt.meso_average_chart(period)
            assert len(bars) == 12 and all(abs(v) < 700 for v in bars), (period, bars)
            assert bars == hunt.chart("meso", period)
        print("MESO_REOPEN_15_MINUTES_NO_SPIKE_PASS hourly=", round(expected))


def test_short_window_not_extrapolated():
    with patch("engine.time.monotonic") as clock:
        clock.return_value = 1000
        hunt = HuntSession(27, "시작")
        hunt.add_meso(50_000)
        clock.return_value = 1001
        hunt.add_meso(50_250)
        assert hunt.meso_session_rate() is None, (
            "250 mesos in one second cannot be extrapolated as 900,000 per hour")
        assert hunt.meso_average_chart(1) == [0.0] * 12
        clock.return_value = 1000 + MIN_MESO_FORECAST_SECONDS
        hunt.add_meso(50_500)
        near(hunt.meso_session_rate(), 500 * 3600 / MIN_MESO_FORECAST_SECONDS)
        assert hunt.initial_meso == 50_000


def test_same_balance_reappearance_and_exp_samples():
    with patch("engine.time.monotonic") as clock:
        clock.return_value = 1000
        hunt = HuntSession(27, "인벤토리")
        hunt.add_meso(10_000)
        clock.return_value = 1060
        hunt.add_meso(10_600)
        near(hunt.meso_session_rate(), 36_000)
        observed_count = len(hunt.meso_sightings)
        # The EXP worker continually samples *cached* meso 10,600 while
        # inventory is not visible. Those aren't new observations.
        for now, exp in ((1100, 3000), (1200, 3300), (1250, 3900)):
            clock.return_value = now
            hunt.add_exp(27, exp, exp / 63666 * 100)
        assert len(hunt.meso_sightings) == observed_count
        near(hunt.meso_session_rate(), 36_000)
        clock.return_value = 1300
        hunt.add_meso(10_600)  # Inventory reopened; MESOS unchanged
        near(hunt.meso_observed_seconds, 300)
        near(hunt.meso_session_rate(), 600 * 3600 / 300)
        near(hunt.rolling_rate("meso", 1), 7200)


def test_negative_net_and_zero_gain():
    with patch("engine.time.monotonic") as clock:
        clock.return_value = 1000
        hunt = HuntSession(27, "판매/구매")
        hunt.add_meso(10000)
        clock.return_value = 1060
        hunt.add_meso(10000)
        near(hunt.meso_session_rate(), 0)
        clock.return_value = 1120
        hunt.add_meso(9000)
        near(hunt.meso_session_rate(), -30000)
        assert all(v <= 0 for v in hunt.meso_average_chart(5)[-2:])


def test_false_31_reading_does_not_create_four_million_per_hour_loss():
    """Regression for the user's v1.4 screenshot: 60,897 -> false '31'."""
    class FakeApp:
        session = None
        meso_pending = None
        meso_pending_time = 0.0
        meso_pending_count = 0
        last_meso_visible_at = None

    ui = FakeApp()
    with patch("app.time.monotonic") as clock:
        clock.return_value = 1000
        ui.session = HuntSession(27, "UI 숫자 검증")
        app.TrackerApp._accept_meso(ui, 60897, 3)
        clock.return_value = 1040
        app.TrackerApp._accept_meso(ui, 31, 3)
        assert ui.session.current_meso == 60897
        clock.return_value = 1044
        app.TrackerApp._accept_meso(ui, 31, 3)
        assert ui.session.current_meso == 60897
        assert ui.session.gained_meso == 0
        assert ui.meso_pending_count == 2
        # The next valid inventory reading clears the suspect amount.
        clock.return_value = 1048
        app.TrackerApp._accept_meso(ui, 60940, 3)
        assert ui.session.current_meso == 60940
        assert ui.session.gained_meso == 43
        assert ui.session.initial_meso == 60897
        assert ui.meso_pending_count == 0
        near(ui.session.meso_session_rate(), 43 * 3600 / 48)
        print("FALSE_31_MESO_BALANCE_GUARD_PASS")


def test_paused_time_is_excluded_from_rate():
    with patch("engine.time.monotonic") as clock:
        clock.return_value = 1000
        hunt = HuntSession(27, "일시정지")
        hunt.add_meso(20000)
        clock.return_value = 1060
        hunt.add_meso(20600)
        clock.return_value = 1070
        hunt.pause()
        assert not hunt.add_meso(999999)
        clock.return_value = 1670
        near(hunt.meso_observed_seconds, 60)
        near(hunt.meso_session_rate(), 36000)
        hunt.resume()
        clock.return_value = 1730
        hunt.add_meso(21200)
        # Active time: 130 seconds, not 730 seconds including pause.
        near(hunt.meso_observed_seconds, 130)
        near(hunt.meso_session_rate(), 1200 * 3600 / 130)


def test_real_gui_uses_session_average_and_keeps_baseline():
    """Exercise actual Tk widgets without accessing the real session DB."""
    real_base, real_config = app.BASE, app.CONFIG_PATH
    try:
        with tempfile.TemporaryDirectory(prefix="meso-average-ui-") as tmp:
            app.BASE = Path(tmp)
            app.CONFIG_PATH = app.BASE / "settings.json"
            app.save_config({"level":27, "map":"가상 테스트", "period":1,
                             "exp_roi":[.515,.945,.642,.985], "always_top":False})
            # Game OCR isn't needed: this QA tests live Tk labels and maths.
            with patch.object(app, "game_info", return_value=None):
                tracker = app.TrackerApp()
            try:
                tracker.withdraw()
                with patch("engine.time.monotonic") as clock:
                    clock.return_value = 2000
                    tracker.new_session()
                    assert tracker.meso_minute.value.cget("text") == "—"
                    tracker._accept_meso(100_000, 3)
                    clock.return_value = 2060
                    tracker._accept_meso(100_600, 3)
                    tracker._render_values()
                    assert tracker.meso_minute.value.cget("text") == "+600"
                    assert tracker.meso_hour.value.cget("text") == "+36,000"
                    assert "100,000" in tracker.meso_baseline_label.cget("text")
                    assert "100,600" in tracker.meso_detail.cget("text")
                    assert "00:01:00" in tracker.meso_hour.sub.cget("text")

                    # Inventory CLOSED: 5min pass, no new MESOS reading.
                    clock.return_value = 2360
                    tracker._render_values()
                    assert tracker.meso_minute.value.cget("text") == "+600"
                    assert tracker.meso_hour.value.cget("text") == "+36,000"

                    # Inventory reopens: 1,200 total gain over 6min.
                    tracker._accept_meso(101_200, 3)
                    tracker._render_values()
                    assert tracker.meso_minute.value.cget("text") == "+200"
                    assert tracker.meso_hour.value.cget("text") == "+12,000"
                    assert tracker.meso_gain_label.cget("text") == "누적 +1,200"
                    assert "100,000" in tracker.meso_baseline_label.cget("text")
                    assert "101,200" in tracker.meso_detail.cget("text")
                    assert "00:06:00" in tracker.meso_hour.sub.cget("text")
                    # Time-window buttons continue to control EXP, not MESOS.
                    tracker.set_period(60)
                    assert tracker.meso_minute.value.cget("text") == "+200"
                    assert tracker.meso_hour.value.cget("text") == "+12,000"
                print("MESO_REAL_TK_BASE_CURRENT_60MIN_TAB_PASS")
            finally:
                tracker.capture_stop.set()
                tracker.window_capture.stop()
                tracker.store.close()
                tracker.destroy()
    finally:
        app.BASE, app.CONFIG_PATH = real_base, real_config


if __name__ == "__main__":
    test_long_closed_inventory_reopening()
    test_short_window_not_extrapolated()
    test_same_balance_reappearance_and_exp_samples()
    test_negative_net_and_zero_gain()
    test_false_31_reading_does_not_create_four_million_per_hour_loss()
    test_paused_time_is_excluded_from_rate()
    test_real_gui_uses_session_average_and_keeps_baseline()
    print("MESO_AVERAGE_FORECAST_ALL_PASS")
