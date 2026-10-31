"""Test 4K zoom, panning, ROI math and real Tk selection interactions."""
from __future__ import annotations

import math
import tempfile
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from exp_picker import ExpRegionPicker, PickerView


DEFAULT = [0.515, 0.945, 0.642, 0.985]


def approximate(actual, expected, tolerance=1e-6):
    assert math.isclose(actual, expected, abs_tol=tolerance), (actual, expected)


def test_model():
    model = PickerView((3840, 2160), (1180, 700))
    assert model.zoom < 0.5
    model.center_on_roi(DEFAULT)
    assert model.zoom > 1.5, model.zoom
    old_cx = (DEFAULT[0] + DEFAULT[2]) / 2 * 3840
    old_cy = (DEFAULT[1] + DEFAULT[3]) / 2 * 2160
    # EXP is at the very bottom of the game. Clamping the source-image edge
    # means it remains visible rather than perfectly vertically centered.
    approx_center = model.to_source(590, 350)
    approximate(approx_center[0], old_cx)
    center_canvas = model.to_canvas(old_cx, old_cy)
    assert 0 < center_canvas[0] < model.viewport_width
    assert 0 < center_canvas[1] < model.viewport_height
    assert model.to_canvas(*approx_center) == (590, 350)

    anchor_canvas = (420, 550)
    original_anchor = model.to_source(*anchor_canvas)
    model.zoom_at(3.0, *anchor_canvas)
    after_anchor = model.to_source(*anchor_canvas)
    for actual, expected in zip(after_anchor, original_anchor):
        approximate(actual, expected)

    region = [0.53, 0.954, 0.635, 0.979]
    corners = ((region[0] * model.width, region[1] * model.height),
               (region[2] * model.width, region[3] * model.height))
    normalized = model.normalized_selection(*corners)
    assert normalized is not None
    for value, expected in zip(normalized, region):
        approximate(value, expected)

    # Drags outside the game image are clipped; tiny drags are ignored.
    assert model.normalized_selection((-90, -60), (50, 30)) == [0, 0, 50 / 3840, 30 / 2160]
    assert model.normalized_selection((100, 100), (101, 102)) is None
    model.pan(-55, 30)
    assert 0 <= model.offset_x <= model.width
    model.resize(960, 550)
    approximate(model.to_source(480, 275)[0],
                model.offset_x + 480 / model.zoom)
    model.fit()
    assert model.zoom < 0.5

    # Different display dimensions and a new EXP ROI still work.
    small = PickerView((1280, 720), (1000, 600))
    small.center_on_roi(DEFAULT)
    assert small.zoom >= 1.0
    print("PICKER_MODEL_4K_AND_720P_PASS")


def test_tk():
    root = tk.Tk()
    root.withdraw()
    snapshot = Image.new("RGB", (3840, 2160), (25, 33, 42))
    chosen = []
    try:
        picker = ExpRegionPicker(root, snapshot, DEFAULT, chosen.append)
        root.update()
        assert picker.view is not None
        picker.render()
        assert picker.view.zoom > 1.5
        assert picker.image_ref is not None
        assert picker.zoom_label.cget("text").endswith("%")
        assert picker.image_ref.width() == picker.view.viewport_width
        assert picker.image_ref.height() == picker.view.viewport_height

        before = picker.view.zoom
        picker.on_wheel(SimpleNamespace(x=500, y=350, delta=120))
        assert picker.view.zoom > before
        picker.on_wheel(SimpleNamespace(x=500, y=350, delta=-120))
        approximate(picker.view.zoom, before)
        picker.pan_begin(SimpleNamespace(x=450, y=350))
        picker.pan_motion(SimpleNamespace(x=460, y=360))
        picker.pan_end(None)
        assert picker.pan_start is None
        picker.focus_exp()

        # Select a smaller original image rectangle, not a resized crop.
        region = [0.54, 0.955, 0.61, 0.973]
        a = picker.view.to_canvas(region[0] * 3840, region[1] * 2160)
        b = picker.view.to_canvas(region[2] * 3840, region[3] * 2160)
        assert 0 <= a[0] < picker.view.viewport_width
        assert 0 <= a[1] < picker.view.viewport_height
        assert 0 <= b[0] < picker.view.viewport_width
        assert 0 <= b[1] < picker.view.viewport_height
        picker.select_start(SimpleNamespace(x=a[0], y=a[1]))
        picker.select_motion(SimpleNamespace(x=b[0], y=b[1]))
        picker.select_finish(SimpleNamespace(x=b[0], y=b[1]))
        assert len(chosen) == 1, chosen
        for value, expected in zip(chosen[0], region):
            approximate(value, expected)
        assert not picker.winfo_exists()

        # ESC/cancel must not overwrite the previous selection.
        other = ExpRegionPicker(root, snapshot, DEFAULT, chosen.append)
        root.update()
        other.destroy()
        assert len(chosen) == 1
        print("PICKER_TK_ZOOM_DRAG_CANCEL_PASS")
    finally:
        root.destroy()


if __name__ == "__main__":
    test_model()
    test_tk()
