"""Regression tests for v1.9.7 EXP misreads and shared capture protection."""
from __future__ import annotations

from unittest.mock import patch

from PIL import Image

from engine import HuntSession, requirement
from screen import read_exp
from window_capture import GameWindowCapture, expanded_exp_roi


def simulated_scan(text: str, level: int = 33, last: int | None = None):
    image = Image.new("RGB", (360, 64), "black")
    with patch("screen._variants", return_value=[("bw", image), ("gray", image), ("color", image)]):
        with patch("screen.recognize", return_value=text):
            return read_exp(image, level, last)


def check_real_world_ocr():
    # Actual corrupt frames from a 3840x2160 user game window.
    for corrupt in (
        "1 13,543 [31 +38+41",
        "1 12,382 [31 +48+41",
        "1 12,370 [31 +47+//",
        "1 12,382 [31 +48+41",
    ):
        assert simulated_scan(corrupt) is None, (corrupt, simulated_scan(corrupt))
        assert simulated_scan(corrupt, last=112929) is None, corrupt

    # A slightly padded crop produces the correct complete number and %.
    for text in ("EXP 113,543 [81 88/+1", "0(2 1 13,543 [81 .88/+]"):
        result = simulated_scan(text)
        assert result is not None, text
        assert result[0] == 113543, (text, result)
        assert abs(result[1] - 81.88) < .01, (text, result)
    assert round(113543 / requirement(33) * 100, 2) == 81.88
    print("REAL_WORLD_OCR_FRAMES_PASS")


def check_capture_margin():
    selected = [0.5392857142857143, 0.9504447268106735,
                0.6178571428571429, 0.974587039390089]
    extended = expanded_exp_roi(selected)
    assert extended[0] < selected[0] and extended[1] < selected[1]
    assert extended[2] > selected[2] and extended[3] > selected[3]
    assert [round(x, 2) for x in selected] == [.54, .95, .62, .97]
    cap = GameWindowCapture(selected)
    assert cap._exp_roi == extended
    assert cap.generation == 0
    cap.set_exp_roi([.52, .94, .63, .98])
    assert cap._exp_roi == expanded_exp_roi([.52, .94, .63, .98])
    cap.stop()
    assert cap.generation == 1
    print("CAPTURE_PADDING_AND_GENERATION_PASS")


def check_no_wild_gain():
    session = HuntSession(33, "QA")
    # Simulate the previously observed fake 1 EXP followed by 112K EXP after
    # seconds. The library rejects the impossible increase even if an OCR
    # caller bypasses normal percentage validation.
    assert session.add_exp(33, 1, 0)
    session.start_clock -= 6
    assert not session.add_exp(33, 112652, 81.24)
    assert session.current_exp == 1 and session.gained_exp == 0
    # Real EXP changes within the level remain accepted.
    assert session.add_exp(33, 521, round(521 / requirement(33) * 100, 2))
    assert session.gained_exp == 520
    print("IMPOSSIBLE_EXP_JUMP_BLOCKED")


if __name__ == "__main__":
    check_real_world_ocr()
    check_capture_margin()
    check_no_wild_gain()
