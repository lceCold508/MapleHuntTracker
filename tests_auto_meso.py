"""Auto-meso detection regression tests; no game keyboard/memory access."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from PIL import Image
from qa_fixture import create_inventory_fixture

from meso_detector import (
    AutoMesoDetector, MesoReading, _line_amount,
    _read_label_without_amount, _read_line_from_result,
)
from screen import ocr_available, game_info, grab_game


def fake_word(text: str, x: int, y: int, w: int, h: int):
    return {
        "text": text,
        "bounding_rect": {"x": x, "y": y, "width": w, "height": h}
    }


def test_parser():
    assert _line_amount("50,792 MESOS") == 50792
    assert _line_amount("8,333,912 MESOS") == 8333912
    assert _line_amount("0 MESOS") == 0
    assert _line_amount("1,234 ME5O5") == 1234
    assert _line_amount("EXP 43,958 (80.07%)") is None
    assert _line_amount("Buy 30,000 mesos each item") is None
    assert _line_amount("123123") is None
    assert _line_amount("20000 50,792 MESOS") is not None


def test_bounding_box():
    line = {
        "text": "50,792 MESOS",
        "words": [fake_word("50,792", 98, 320, 60, 20),
                  fake_word("MESOS", 161, 321, 58, 20)]
    }
    r = _read_line_from_result({"lines": [line]}, (1280, 720))
    assert r and r.amount == 50792 and r.confidence == 3
    assert 0 <= r.region[0] < r.region[2] <= 1
    assert r.region[0] * 1280 < 98
    assert r.region[2] * 1280 > 200
    no_num = {"lines": [{"text": "MESOS", "words": [fake_word("MESOS", 160, 320, 58, 20)]}]}
    assert _read_line_from_result(no_num, (1280, 720)) is None
    assert _read_label_without_amount(no_num, (1280, 720))


def create_fixture(path: Path, x: int, y: int) -> Image.Image:
    image = create_inventory_fixture(x=x, y=y, amount=50792)
    image.save(path)
    return image


def test_synthetic_ocr():
    assert ocr_available(), "Windows OCR engine unavailable"
    # Use actual Windows OCR for a full-screen and relocatable inventory.
    work = Path(__file__).resolve().parent / "debug"
    work.mkdir(exist_ok=True)
    for x, y in ((10, 60), (975, 100)):
        img = create_fixture(work / f"auto_meso_fixture_{x}.png", x, y)
        detector = AutoMesoDetector()
        actual = detector.detect(img)
        if not actual or actual.amount != 50792:
            raise AssertionError(f"Auto detection failed for moved inventory {x,y}: {actual}")
        assert detector.detect(img).amount == 50792
    print("MESO_AUTO_SYNTHETIC_OCR_PASS left_and_relocated_inventory=true")


def test_closed_inventory():
    info = game_info()
    if info is None:
        return
    image = grab_game(info)
    result = AutoMesoDetector().detect(image)
    if result:
        # If user happened to open inventory, report but don't assume a
        # false positive. Real inventory is allowed to be present.
        print("MESO_LIVE_INVENTORY_DETECTED", result.amount)
    else:
        print("MESO_LIVE_INVENTORY_NOT_VISIBLE_AS_EXPECTED")


def test_real_classic_inventory_ocr_replay():
    """Regression: real Global Classic pixels OCR number and label separately.

    The old detector incorrectly returned None although the footer contained
    a valid '60,464'. The title-anchored parser must identify this amount.
    """
    folder = Path(__file__).resolve().parent / "debug"
    cropped = folder / "inventory_zoom_live.png"
    backing = folder / "maple_window_wgc.png"
    if not cropped.is_file() or not backing.is_file():
        return
    image = Image.open(backing).convert("RGB")
    image.paste(Image.open(cropped).convert("RGB"), (1000, 0))
    detector = AutoMesoDetector()
    reading = detector.detect(image)
    assert reading is not None and reading.amount == 60464, reading
    assert reading.confidence >= 3
    assert "inventory_title" in reading.method
    print("REAL_CLASSIC_MESO_OCR_REPLAY_PASS amount=60464")


def test_wrong_title_height_cannot_read_item_count():
    """OCR title height jitter must not move the MESOS crop into item cells."""
    from meso_detector import _recognize, _read_from_inventory_title
    img_path = Path(__file__).resolve().parent / "debug" / "inventory_zoom_live.png"
    if not img_path.is_file():
        return
    image = Image.open(img_path).convert("RGB")
    result = _recognize(image)
    for line in result.get("lines", []):
        if "ITEM INVENTORY" in str(line.get("text", "")).upper():
            for word in line.get("words", []):
                # Title width stays correct; Windows OCR occasionally
                # shrinks glyph-height estimates to 10 px, causing the
                # v1.4 crop to read e.g. a 31-item stack, not mesos.
                word["bounding_rect"]["height"] = 9.0
    reading = _read_from_inventory_title(image, result, image.size)
    assert reading and reading.amount == 60464, reading
    # Remove only the MESOS label pixels. A fake item count '31' in the
    # inventory must not be allowed to masquerade as a footer balance.
    masked = image.copy()
    from PIL import ImageDraw
    draw = ImageDraw.Draw(masked)
    draw.rectangle((612, 742, 725, 807), fill=(151, 191, 216))
    assert _read_from_inventory_title(masked, result, image.size) is None
    print("MESO_LABEL_AND_TITLE_GEOMETRY_GUARD_PASS")


if __name__ == "__main__":
    test_parser()
    test_bounding_box()
    test_synthetic_ocr()
    test_closed_inventory()
    test_real_classic_inventory_ocr_replay()
    test_wrong_title_height_cannot_read_item_count()
    print("MESO_AUTO_TESTS_PASS")
