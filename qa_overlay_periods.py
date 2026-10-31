"""V1.9 timed EXP and Haste status visual regression tests."""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from PIL import ImageGrab

import overlay_app as overlay
from timed_exp import ExpTimerSet, LABELS, TIMERS


def test_timers():
    run = ExpTimerSet()
    assert run.display(6, 0)[2] == "waiting"
    assert run.observe(elapsed=20.0, total_exp=10000) == []
    assert run.passed(20.0) == 0
    assert run.display(6, 20)[1] == "06:00"

    # Constant 10 EXP per second, all four timers start together.
    completed = []
    for second in range(1, 3601):
        completed += run.observe(elapsed=20 + second,
                                 total_exp=10000 + second * 10)
    assert set(result.minutes for result in completed) == {6, 10, 30, 60}
    assert {minutes: run.results[minutes].exp for minutes, _ in TIMERS} == {
        6: 3600, 10: 6000, 30: 18000, 60: 36000,
    }
    frozen = dict(run.results)
    run.observe(elapsed=20 + 4000, total_exp=50000)
    assert run.results == frozen
    for minutes, _ in TIMERS:
        text, status, state = run.display(minutes, 4000)
        assert state == "complete", (minutes, text, status)
        assert status == "완료"

    # Timer counts active session elapsed only. Caller freezes elapsed while paused.
    paused = ExpTimerSet()
    paused.observe(elapsed=0, total_exp=1000)
    paused.observe(elapsed=200, total_exp=2000)
    assert paused.display(6, 200)[1] == "02:40"
    assert not paused.results

    # No fabricated total when OCR was missing near the checkpoint.
    lost = ExpTimerSet()
    lost.observe(elapsed=0, total_exp=100)
    lost.observe(elapsed=345, total_exp=3550)
    lost.observe(elapsed=370, total_exp=3800)
    assert lost.results[6].status == "missing_scan"
    assert lost.results[6].exp is None
    assert lost.display(6, 370)[2] == "missing_scan"
    print("EXP_TIMERS_PASS", ",".join(LABELS[minutes] for minutes, _ in TIMERS))


def test_overlay():
    output = Path(__file__).parent / "debug"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="maple-timed-exp-qa-") as folder:
        overlay.BASE = Path(folder)
        overlay.CONFIG_PATH = overlay.BASE / "settings.json"
        app = overlay.OverlayApp(start_worker=False, start_session=True)
        try:
            hunt = app.session
            assert hunt is not None
            base = 44607
            assert hunt.add_exp(31, base, 41.12)
            base_total = hunt.latest_total_exp
            for elapsed in range(0, 611):
                app.timers.observe(elapsed=elapsed, total_exp=base_total + elapsed * 10)
            # Give the displayed session a realistic EXP rate & 10m progress.
            hunt.start_clock -= 610
            assert hunt.add_exp(31, base + 6100, round((base+6100) / 108480 * 100, 2))
            app.last_valid = time.monotonic()
            app._show_values()
            app.update()
            assert app.timers.results[6].exp == 3600
            assert app.timers.results[10].exp == 6000
            assert app.timer_widgets[6][0].cget("text") == "+3,600 EXP"
            assert app.timer_widgets[10][0].cget("text") == "+6,000 EXP"
            assert app.timer_widgets[6][1].cget("text") == "완료"
            assert app.timer_widgets[30][1].cget("text") == "19:50"
            assert app.timer_widgets[60][1].cget("text") == "49:50"
            assert not hasattr(app, "period_buttons")
            assert "시급" not in app.hour_text.cget("text")

            checks = [app.level_text, app.minute_heading, app.minute_text,
                      app.required_text, app.remaining_text, app.hour_text,
                      app.gain_text, app.haste_status_text, app.haste_sound_text,
                      app.eta_text, app.timer_text,
                      app.new_session_button]
            for number, clock in app.timer_widgets.values():
                checks.extend((number, clock))
            for control in checks:
                msg = control.cget("text")
                assert control.winfo_ismapped(), msg
                assert control.winfo_reqwidth() <= control.winfo_width() + 2, (
                    msg, control.winfo_reqwidth(), control.winfo_width())
                left, top = control.winfo_rootx(), control.winfo_rooty()
                right = left + control.winfo_width()
                bottom = top + control.winfo_height()
                assert left >= app.winfo_rootx(), msg
                assert top >= app.winfo_rooty(), msg
                assert right <= app.winfo_rootx() + overlay.WIDTH, msg
                assert bottom <= app.winfo_rooty() + overlay.HEIGHT, msg

            x, y = app.winfo_rootx(), app.winfo_rooty()
            ImageGrab.grab(bbox=(x, y, x + overlay.WIDTH,
                                 y + overlay.HEIGHT), all_screens=True).save(
                output / "v19-haste-buff-overlay.png")

            # Finished records survive a new session and don't recompute.
            old = hunt
            app.new_session_button.invoke()
            assert old.stopped
            assert app.timers.anchor_total is None
            assert app.session.gained_exp == 0
            assert app.store.recent()[1]["ended"] is not None
            records = [json.loads(line) for line in
                       app.timer_log_path.read_text(encoding="utf-8").splitlines()]
            session_end = [entry for entry in records if entry.get("kind") == "session_end"]
            assert session_end
            assert session_end[-1]["timers"]["6분"]["exp"] == 3600
            assert session_end[-1]["timers"]["10분"]["exp"] == 6000
            print("TIMED_OVERLAY_QA_PASS", overlay.WIDTH, overlay.HEIGHT)
        finally:
            app.close()


if __name__ == "__main__":
    test_timers()
    test_overlay()
