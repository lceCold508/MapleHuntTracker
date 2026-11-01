"""Regression checks for an opaque-text, fully transparent Maple HUD."""
from __future__ import annotations

import ctypes
import json
import tempfile
import tkinter as tk
from pathlib import Path

import overlay_app as overlay


def check_configuration():
    with tempfile.TemporaryDirectory(prefix="maple-hud-config-") as folder:
        overlay.BASE = Path(folder)
        overlay.CONFIG_PATH = overlay.BASE / "settings.json"
        defaults = overlay.load_config()
        assert defaults["transparent_bg"] is True
        assert defaults["overlay_alpha"] == .55

        # Old persisted v1.9.7 preference should migrate to the new default
        # without erasing level, selected EXP region or any other property.
        old = {"level": 33, "overlay_alpha": .83,
               "exp_roi": [.5, .9, .7, .98], "always_top": True}
        overlay.CONFIG_PATH.write_text(json.dumps(old), encoding="utf-8")
        upgraded = overlay.load_config()
        assert upgraded["level"] == 33
        assert upgraded["exp_roi"] == old["exp_roi"]
        assert upgraded["transparent_bg"] is True
        assert upgraded["overlay_alpha"] == .55
        upgraded["transparent_bg"] = False
        upgraded["overlay_alpha"] = .75
        overlay.save_config(upgraded)
        assert overlay.load_config()["transparent_bg"] is False
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

            # The quick mode button must restore the old card backgrounds,
            # then return to completely transparent without losing text.
            app.background_button.event_generate("<Button-1>")
            app.update()
            assert app.config_data["transparent_bg"] is False
            assert app.attributes("-transparentcolor") == ""
            assert app.attributes("-alpha") == .55
            assert app.minute_text.cget("bg") == overlay.BG
            assert app.haste_status_text.cget("bg") == overlay.HEADER
            assert app.new_session_button.cget("bg") == "#315943"
            assert app.level_text.cget("fg") == overlay.FG
            app.background_button.event_generate("<Button-1>")
            app.update()
            assert app.config_data["transparent_bg"] is True
            assert app.attributes("-alpha") == 1.0
            assert app.attributes("-transparentcolor").lower() == overlay.TRANSPARENT_KEY
            assert app.new_session_button.cget("bg") == overlay.TRANSPARENT_KEY
            assert overlay.load_config()["transparent_bg"] is True

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
