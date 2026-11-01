"""Regression checks for readable dark backdrop + fully opaque HUD text."""
from __future__ import annotations

import ctypes
import json
import tempfile
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace

import overlay_app as overlay


def check_configuration():
    # 55%-opaque backdrop: previously grey labels (#97a8b6) looked faded.
    # Require almost-white secondary text while leaving label alpha at 255.
    secondary = overlay.MUTED.lstrip("#")
    channels = [int(secondary[i:i+2], 16) for i in (0, 2, 4)]
    assert min(channels) >= 235, (overlay.MUTED, channels)
    with tempfile.TemporaryDirectory(prefix="maple-hud-config-") as folder:
        overlay.BASE = Path(folder)
        overlay.CONFIG_PATH = overlay.BASE / "settings.json"
        defaults = overlay.load_config()
        assert defaults["backdrop_enabled"] is True
        assert defaults["overlay_alpha"] == .55

        # Old persisted v1.9.7 preference should migrate to the new default
        # without erasing level, selected EXP region or any other property.
        old = {"level": 33, "overlay_alpha": .83,
               "exp_roi": [.5, .9, .7, .98], "always_top": True}
        overlay.CONFIG_PATH.write_text(json.dumps(old), encoding="utf-8")
        upgraded = overlay.load_config()
        assert upgraded["level"] == 33
        assert upgraded["exp_roi"] == old["exp_roi"]
        assert upgraded["backdrop_enabled"] is True
        assert upgraded["overlay_alpha"] == .55
        upgraded["backdrop_enabled"] = False
        upgraded["overlay_alpha"] = .75
        overlay.save_config(upgraded)
        assert overlay.load_config()["backdrop_enabled"] is False
        assert overlay.load_config()["overlay_alpha"] == .75
        print("HUD_CONFIG_MIGRATION_PASS")


def check_window():
    with tempfile.TemporaryDirectory(prefix="maple-hud-alpha-") as folder:
        overlay.BASE = Path(folder)
        overlay.CONFIG_PATH = overlay.BASE / "settings.json"
        app = overlay.OverlayApp(start_worker=False, start_session=False)
        try:
            app.update()
            assert app.attributes("-alpha") == 1.0
            assert app.attributes("-transparentcolor").lower() == overlay.TRANSPARENT_KEY
            assert app.cget("bg") == overlay.TRANSPARENT_KEY
            assert app._backdrop_window is not None
            assert app._backdrop_window.state() == "normal"
            assert app._backdrop_window.attributes("-alpha") == .55
            assert app._backdrop_window.attributes("-transparentcolor") == ""
            assert app._backdrop_window.winfo_x() == app.winfo_x()
            assert app._backdrop_window.winfo_y() == app.winfo_y()
            app._paint_backdrop()
            assert len(app._backdrop_canvas.find_all()) >= 8
            for widget, original in app._hud_original_styles:
                assert widget.cget("background").lower() == overlay.TRANSPARENT_KEY, widget
                if "highlightthickness" in original:
                    assert int(widget.cget("highlightthickness")) == 0, widget
                if "borderwidth" in original:
                    assert int(widget.cget("borderwidth")) == 0, widget

            for widget in (app.minute_text, app.level_text,
                           app.hour_text, app.haste_status_text,
                           app.new_session_button):
                assert widget.cget("fg") != overlay.TRANSPARENT_KEY
                assert widget.cget("bg") == overlay.TRANSPARENT_KEY
            for label in (app.minute_heading, app.required_text,
                          app.gain_text, app.eta_text,
                          app.timer_text):
                assert label.cget("fg") == overlay.MUTED, label
                assert label.cget("bg") == overlay.TRANSPARENT_KEY, label
            assert app.haste_sound_text.cget("fg") in (overlay.MUTED, overlay.ACCENT)
            for number, clock in app.timer_widgets.values():
                assert clock.cget("fg") == overlay.MUTED
            print("SECONDARY_LABELS_FULL_BRIGHTNESS_PASS", overlay.MUTED)

            # Check the *real Windows HWND*, not just Tk option strings.
            dwm_key = ctypes.c_uint32()
            dwm_alpha = ctypes.c_ubyte()
            dwm_flags = ctypes.c_uint32()
            query = ctypes.windll.user32.GetLayeredWindowAttributes
            query.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                              ctypes.c_void_p, ctypes.c_void_p]
            query.restype = ctypes.c_bool
            # Tk's winfo_id() is the child/client HWND. The layered-window
            # flags belong to its native Windows wrapper HWND (GA_ROOT).
            get_root = ctypes.windll.user32.GetAncestor
            get_root.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            get_root.restype = ctypes.c_void_p
            parent_hwnd = get_root(app.winfo_id(), 2)
            assert parent_hwnd
            assert query(parent_hwnd, ctypes.byref(dwm_key),
                         ctypes.byref(dwm_alpha), ctypes.byref(dwm_flags))
            assert dwm_flags.value & 0x1, dwm_flags.value  # LWA_COLORKEY
            # Tk might set the alpha flag as well, but 255 is fully opaque.
            assert not (dwm_flags.value & 0x2) or dwm_alpha.value == 255
            print("HUD_WINDOWS_COLORKEY_AND_OPAQUE_TEXT_PASS",
                  hex(dwm_key.value), dwm_flags.value)

            # The separate shaded background is alpha blended, and does not
            # accept mouse input or activation: the game's controls survive.
            bg_hwnd = get_root(app._backdrop_window.winfo_id(), 2)
            assert bg_hwnd and bg_hwnd != parent_hwnd
            bg_color, bg_alpha, bg_flags = (ctypes.c_uint32(), ctypes.c_ubyte(),
                                            ctypes.c_uint32())
            assert query(bg_hwnd, ctypes.byref(bg_color),
                         ctypes.byref(bg_alpha), ctypes.byref(bg_flags))
            assert bg_flags.value & 0x2  # LWA_ALPHA
            assert 138 <= bg_alpha.value <= 142, bg_alpha.value  # 55%
            get_extended = ctypes.windll.user32.GetWindowLongPtrW
            get_extended.argtypes = [ctypes.c_void_p, ctypes.c_int]
            get_extended.restype = ctypes.c_ssize_t
            bg_extended = get_extended(bg_hwnd, -20)
            assert bg_extended & 0x20  # WS_EX_TRANSPARENT
            assert bg_extended & 0x08000000  # WS_EX_NOACTIVATE
            print("HUD_DARK_BACKDROP_ALPHA_AND_CLICKTHROUGH_PASS", bg_alpha.value)

            # Quick button hides/shows the shaded background ONLY; never
            # alters foreground text alpha or transparency key.
            app.background_button.event_generate("<Button-1>")
            app.update()
            assert app.config_data["backdrop_enabled"] is False
            assert app._backdrop_window.state() == "withdrawn"
            assert app.attributes("-alpha") == 1.0
            assert app.minute_text.cget("bg") == overlay.TRANSPARENT_KEY
            assert app.haste_status_text.cget("bg") == overlay.TRANSPARENT_KEY
            assert app.level_text.cget("fg") == overlay.FG
            app.background_button.event_generate("<Button-1>")
            app.update()
            assert app.config_data["backdrop_enabled"] is True
            assert app.attributes("-alpha") == 1.0
            assert app.attributes("-transparentcolor").lower() == overlay.TRANSPARENT_KEY
            assert app._backdrop_window.state() == "normal"
            assert app.new_session_button.cget("bg") == overlay.TRANSPARENT_KEY
            assert overlay.load_config()["backdrop_enabled"] is True

            # Dragging the foreground must move its shaded backing exactly.
            x, y = app.winfo_x(), app.winfo_y()
            app._drag_start(SimpleNamespace(x_root=x+80, y_root=y+35))
            app._drag_move(SimpleNamespace(x_root=x+130, y_root=y+75))
            app.update_idletasks()
            assert app.winfo_x() == x+50 and app.winfo_y() == y+40
            assert app._backdrop_window.winfo_x() == app.winfo_x()
            assert app._backdrop_window.winfo_y() == app.winfo_y()
            app._drag_end(None)
            app._apply_backdrop(True, opacity=.75)
            assert app._backdrop_window.attributes("-alpha") == .75
            assert app.attributes("-alpha") == 1.0
            app._apply_backdrop(True, opacity=.55)

            # Settings & EXP chooser remain separate opaque Toplevel windows.
            app.show_settings()
            app.update()
            settings = app._settings_window
            assert settings is not None and settings.winfo_exists()
            assert settings.cget("bg") == overlay.BG
            assert settings.attributes("-transparentcolor") == ""
            settings.destroy()
            print("HUD_TOGGLE_AND_SETTINGS_OPAQUE_PASS")
        finally:
            app.close()


if __name__ == "__main__":
    check_configuration()
    check_window()
