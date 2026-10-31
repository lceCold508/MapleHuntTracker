from __future__ import annotations

import tempfile
from pathlib import Path
from engine import HuntSession, SessionStore, accumulated, requirement, fmt_hms, fmt_int
from screen import parse_exp_text, parse_meso_text, read_exp, ocr_available
from PIL import Image


def test_units():
    assert requirement(26) == 54900
    assert accumulated(26, 19497) == 292768
    assert parse_exp_text("EXP 19,497[35.51%]") == (19497, 35.51)
    assert parse_exp_text("19,497[35.51 %]") == (19497, 35.51)
    assert parse_exp_text("25,254[46.00/+]") == (25254, 46.00)
    assert parse_exp_text("EXP 25,254 (46.00/+1") == (25254, 46.00)
    assert parse_exp_text("21 , 838[39.78%]") == (21838, 39.78)
    assert parse_exp_text("0(2 52,845[96.26/+]") == (52845, 96.26)
    assert parse_exp_text("3,609 [5* 67%]") == (3609, 5.67)
    assert parse_exp_text("EXR 4,265 (6 70/+1") == (4265, 6.70)
    assert parse_meso_text("메소 1,245,987") == 1245987
    assert parse_meso_text("Meso 765,432") == 765432
    assert fmt_int(1234567) == "1,234,567"
    assert fmt_hms(3723) == "01:02:03"


def test_session():
    hunt = HuntSession(26, "아랫마을")
    hunt.start_clock -= 60
    assert hunt.add_exp(26, 19000, 34.61)
    assert hunt.add_meso(100000)
    hunt.start_clock -= 60
    assert hunt.add_exp(26, 21300, 38.80)
    assert hunt.add_meso(112300)
    assert hunt.gained_exp == 2300
    assert hunt.gained_meso == 12300
    assert hunt.rolling_rate("exp", 5) is not None
    assert hunt.until_level(hunt.rolling_rate("exp", 5)) is not None
    assert hunt.add_exp(27, 1200, 1.88)
    assert hunt.gained_exp == 54900 - 19000 + 1200
    assert hunt.add_exp(27, 1200, 1.88)
    hunt.pause()
    assert not hunt.add_exp(27, 3000, 4.71)
    with tempfile.TemporaryDirectory() as td:
        store = SessionStore(Path(td))
        store.begin(hunt)
        store.save(hunt)
        store.finish(hunt)
        assert len(store.recent()) == 1
        path = Path(td) / "history.csv"
        assert store.export(path) == 1
        assert "아랫마을".encode("utf-8") in path.read_bytes()
        store.close()


def test_ocr_saved_image():
    test_img = Path(__file__).parent / "debug" / "exp_orig.png"
    if not test_img.exists():
        return
    assert ocr_available()
    result = read_exp(Image.open(test_img), 26)
    assert result and result[0] == 19497 and abs(result[1] - 35.51) < .1, result
    # Previously, Windows OCR split '11.80%' as '1 1 *80%' and the
    # session stopped counting EXP entirely. Validate real game pixels.
    real = Path(__file__).parent / "debug" / "maple_window_wgc_exp.png"
    if real.is_file():
        actual = read_exp(Image.open(real), 27)
        assert actual and actual[0] == 7514 and abs(actual[1]-11.80)<.03, actual


def test_auto_meso_session_baseline_and_gains():
    from app import TrackerApp

    class UiState:
        session = HuntSession(26, "개미굴")
        meso_pending = None
        meso_pending_time = 0.0
        last_meso_visible_at = None

    ui = UiState()
    TrackerApp._accept_meso(ui, 50_792, confidence=3)
    assert ui.session.initial_meso == 50_792
    assert ui.session.current_meso == 50_792
    assert ui.session.gained_meso == 0
    # Mesos gained on every scan need not be identical across two snapshots.
    TrackerApp._accept_meso(ui, 51_100, confidence=3)
    TrackerApp._accept_meso(ui, 51_480, confidence=3)
    assert ui.session.current_meso == 51_480
    assert ui.session.gained_meso == 688
    TrackerApp._accept_meso(ui, 49_480, confidence=3)
    assert ui.session.gained_meso == -1312
    ui.session.pause()
    TrackerApp._accept_meso(ui, 99_999, confidence=3)
    assert ui.session.current_meso == 49_480
    # New session must establish its own first observed MESOS baseline.
    ui.session = HuntSession(26, "개미굴")
    ui.session.add_meso(25_000)
    assert ui.session.gained_meso == 0


def test_auto_detected_starting_meso_is_preserved():
    from app import TrackerApp
    class FakeUi:
        session = HuntSession(27, "게임 화면")
        meso_pending = None
        meso_pending_time = 0.0
        last_meso_visible_at = None
    ui = FakeUi()
    TrackerApp._accept_meso(ui, 60464, 3)
    assert ui.session.initial_meso == 60464
    assert ui.session.current_meso == 60464
    TrackerApp._accept_meso(ui, 60999, 3)
    assert ui.session.initial_meso == 60464, "Baseline changed during session"
    assert ui.session.current_meso == 60999
    assert ui.session.gained_meso == 535


if __name__ == "__main__":
    test_units()
    test_session()
    test_ocr_saved_image()
    test_auto_meso_session_baseline_and_gains()
    test_auto_detected_starting_meso_is_preserved()
    print("MAPLE_HUNT_TESTS_PASS: OCR + exp calculation + meso + level-up + SQLite + CSV")
