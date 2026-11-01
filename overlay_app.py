"""Compact EXP-only overlay for MapleStory Classic World (v1.6).

The existing v1.5 OCR, game-window capture, and session engine are reused.
Only visible pixels are read; no game memory, packets or input hooks.
"""
from __future__ import annotations

import ctypes
import json
import math
import os
import queue
import sys
import tempfile
import threading
import time
import traceback
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox

from PIL import Image, ImageTk

from engine import HuntSession, SessionStore, eta_text, fmt_int, requirement
from screen import (
    DEFAULT_EXP_REGION, capture_region, crop_game_region, game_info, grab_game,
    ocr_available, parse_exp_text, read_exp,
)
from window_capture import GameWindowCapture
from timed_exp import ExpTimerSet, TIMERS, LABELS
from haste_buff import HasteMatcher, HasteState, beep_haste_expired
from exp_picker import ExpRegionPicker
from diagnostics import DiagnosticJournal


VERSION = "1.9.8"
APP_NAME = "글클메 사냥 트래커"
BASE = Path(os.environ.get("MAPLE_TRACKER_DATA_DIR") or
            (Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "MapleHuntTracker"))
CONFIG_PATH = BASE / "settings.json"

WIDTH, HEIGHT = 460, 485
POLL_INTERVAL = 0.50
UI_INTERVAL_MS = 100
BG = "#111923"
HEADER = "#1a2633"
# Windows' layered-window chroma key makes only the backdrop invisible.
# Text and buttons remain at 100% opacity (unlike wm_attributes('-alpha')).
TRANSPARENT_KEY = "#010203"
FG = "#f4f7fa"
MUTED = "#97a8b6"
ACCENT = "#8efaac"
WARNING = "#ffbf70"
ERROR = "#ff7b88"


def load_config() -> dict:
    defaults = {
        "level": 31, "map": "사냥터 미지정",
        "exp_roi": list(DEFAULT_EXP_REGION),
        "always_top": True, "overlay_alpha": 0.55,
        "transparent_bg": True,
        "auto_rescan": True, "haste_alert": True,
    }
    try:
        value = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            defaults.update(value)
            # On upgrade, default to the user's preferred minimum backdrop
            # opacity in case they explicitly switch off full transparency.
            if "transparent_bg" not in value:
                defaults["overlay_alpha"] = 0.55
    except (OSError, ValueError, TypeError):
        pass
    return defaults


def save_config(config: dict) -> None:
    BASE.mkdir(parents=True, exist_ok=True)
    temp = CONFIG_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(CONFIG_PATH)


def clamp_alpha(value) -> float:
    try:
        return min(1.0, max(0.55, float(value)))
    except (ValueError, TypeError):
        return 0.55


def experience_progress(level: int, current_exp: int | None) -> tuple[int | None, int | None]:
    """Full EXP requirement and remaining EXP until the next level."""
    needed = requirement(level)
    if needed is None:
        return None, None
    if current_exp is None or not 0 <= current_exp <= needed:
        return needed, None
    return needed, needed - current_exp


def exp_rejection_reason(hunt: HuntSession, level: int, exp: int) -> str | None:
    """Reject likely OCR errors before they reach the session/rate engine."""
    if exp == 0 and (hunt.current_exp is None or level == hunt.level):
        return "EXP 0 오인식 무시"
    if level < hunt.level:
        return "이전 레벨 OCR 무시"
    if level == hunt.level and hunt.current_exp is not None and exp < hunt.current_exp:
        return "EXP 감소 오인식 무시"
    return None


def virtual_screen() -> tuple[int, int, int, int]:
    user32 = ctypes.windll.user32
    x, y = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
    return x, y, x + user32.GetSystemMetrics(78), y + user32.GetSystemMetrics(79)


def safe_position(saved, game=None) -> tuple[int, int]:
    vx, vy, vr, vb = virtual_screen()
    if isinstance(saved, list) and len(saved) == 2:
        try:
            x, y = int(saved[0]), int(saved[1])
            if vx <= x <= vr - WIDTH and vy <= y <= vb - HEIGHT:
                return x, y
        except (TypeError, ValueError):
            pass
    if game is not None:
        gx1, gy1, gx2, gy2 = game.bbox
        if gx2 - gx1 >= WIDTH + 20 and gy2 - gy1 >= HEIGHT + 20:
            return max(vx, min(gx2 - WIDTH - 18, vr - WIDTH)), max(vy, gy1 + 42)
    return max(vx, vr - WIDTH - 60), max(vy, vy + 80)


class RecoveryPolicy:
    """Stale/invalid OCR gets an automatic, throttled capture restart."""

    def __init__(self):
        self.failures = 0
        self.last_restart = -1e9

    def success(self):
        self.failures = 0

    def failure(self, now: float, last_valid: float | None,
                enabled: bool = True) -> bool:
        self.failures += 1
        stale = last_valid is None or now - last_valid >= 3.0
        if (enabled and self.failures >= 3 and stale and
                now - self.last_restart >= 7.0):
            self.last_restart = now
            self.failures = 0
            return True
        return False


class OverlayApp(tk.Tk):
    def __init__(self, *, start_worker: bool = True, start_session: bool = True):
        super().__init__()
        self.withdraw()
        self.title(f"{APP_NAME} · {VERSION}")
        self.configure(bg=BG)
        self.overrideredirect(True)
        self.config_data = load_config()
        self.reading_level = int(self.config_data.get("level", 31))
        self.exp_roi = self.config_data.get("exp_roi", list(DEFAULT_EXP_REGION))
        self.capture = GameWindowCapture(self.exp_roi)
        self.diagnostics = DiagnosticJournal(BASE)
        self.haste_monitor = HasteState()
        self.haste_status = "waiting"
        self.haste_confidence = 0.0
        self.last_haste_sample_at: float | None = None
        self.haste_matcher_error = ""
        try:
            self.haste_matcher = HasteMatcher()
        except (OSError, ValueError, RuntimeError) as error:
            self.haste_matcher = None
            self.haste_status = "error"
            self.haste_matcher_error = str(error)
        self.store = SessionStore(BASE)
        self.session: HuntSession | None = None
        self.timers = ExpTimerSet()
        self.timer_log_path = BASE / "exp_timer_history.jsonl"
        self.event_queue: queue.SimpleQueue = queue.SimpleQueue()
        self.stop_event = threading.Event()
        self.rescan_event = threading.Event()
        self.last_valid: float | None = None
        self.last_sample_raw = ""
        self.ocr_error = ""
        self.scan_status = "게임 화면 감지 중"
        self.retry_count = 0
        self.worker = None
        self.haste_worker = None
        self.last_save = 0.0
        self.last_redraw = 0.0
        self._drag_origin = None
        self._settings_window = None
        self._picker_window = None
        self._closing = False
        self._hud_original_styles = []
        x, y = safe_position(self.config_data.get("overlay_geometry"), game_info())
        self.geometry(f"{WIDTH}x{HEIGHT}+{x}+{y}")
        self.attributes("-topmost", bool(self.config_data.get("always_top", True)))
        self._make_ui()
        self._remember_hud_styles()
        self._apply_hud_transparency(bool(self.config_data.get("transparent_bg", True)))
        self.bind("<Button-3>", self._open_menu)
        self.bind("<Control-r>", lambda _: self.rescan())
        self.bind("<Escape>", lambda _: None)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.deiconify()
        self._show_values()
        if start_session:
            self.start_new_session()
        if start_worker:
            self.worker = threading.Thread(target=self._scan_worker, daemon=True,
                                           name="Maple-EXP-Overlay-OCR")
            self.worker.start()
            if self.haste_matcher is not None:
                self.haste_worker = threading.Thread(
                    target=self._haste_worker, daemon=True, name="Maple-Haste-HUD-Scan")
                self.haste_worker.start()
        self.after(UI_INTERVAL_MS, self._tick)

    def _font(self, size, weight="normal"):
        return ("Malgun Gothic", size, weight)

    def _label(self, parent, text, size=10, color=FG, bold=False, bg=BG):
        return tk.Label(parent, text=text, font=self._font(size, "bold" if bold else "normal"),
                        fg=color, bg=bg, bd=0, padx=0, pady=0)

    def _icon_button(self, parent, text, command, *, color=MUTED):
        # Hangul action labels avoid font-dependent icons appearing as boxes.
        control = self._label(parent, text, size=10, color=color, bold=True, bg=HEADER)
        control.configure(width=max(4, len(text) + 1), cursor="hand2")
        control.bind("<Button-1>", lambda _: command())
        control.bind("<Enter>", lambda _: control.configure(fg=FG))
        control.bind("<Leave>", lambda _: control.configure(fg=color))
        return control

    def _drag_bind(self, widget):
        widget.bind("<ButtonPress-1>", self._drag_start)
        widget.bind("<B1-Motion>", self._drag_move)
        widget.bind("<ButtonRelease-1>", self._drag_end)
        widget.bind("<Button-3>", self._open_menu)

    def _remember_hud_styles(self):
        """Remember just overlay widget styles; settings windows stay solid."""
        self._hud_original_styles = []

        def visit(parent):
            for widget in parent.winfo_children():
                if isinstance(widget, tk.Toplevel):
                    continue
                available = widget.configure()
                original = {}
                for option in ("background", "activebackground", "highlightbackground",
                               "highlightcolor", "highlightthickness", "borderwidth"):
                    if option in available:
                        original[option] = widget.cget(option)
                self._hud_original_styles.append((widget, original))
                visit(widget)

        visit(self)

    def _apply_hud_transparency(self, transparent: bool, opacity: float | None = None):
        """Hide ONLY the HUD background; keep RGB text opaque and readable.

        On Windows, -transparentcolor makes only pixels with the matching
        background RGB transparent. -alpha must be 1 to keep EXP labels crisp.
        Reverting to legacy mode restores original frame/card fills.
        """
        self.attributes("-transparentcolor", "")
        self.attributes("-alpha", 1.0)
        self.configure(bg=TRANSPARENT_KEY if transparent else BG)
        for widget, original in self._hud_original_styles:
            if not widget.winfo_exists():
                continue
            opts = dict(original)
            if transparent:
                for field in ("background", "activebackground", "highlightbackground",
                              "highlightcolor"):
                    if field in opts:
                        opts[field] = TRANSPARENT_KEY
                for field in ("highlightthickness", "borderwidth"):
                    if field in opts:
                        opts[field] = 0
            if opts:
                widget.configure(**opts)
        if transparent:
            # Set the chroma key last so widget updates never flash over the
            # game. Tk's normal Toplevel dialogs use their own opaque HWNDs.
            self.attributes("-transparentcolor", TRANSPARENT_KEY)
        else:
            self.attributes("-alpha", clamp_alpha(
                self.config_data.get("overlay_alpha", 0.55) if opacity is None
                else opacity))

    def _make_ui(self):
        outer = tk.Frame(self, bg=BG, highlightbackground="#354554", highlightthickness=1)
        outer.pack(fill="both", expand=True)
        header = tk.Frame(outer, bg=HEADER, height=38)
        header.pack(fill="x")
        header.pack_propagate(False)
        self.level_text = self._label(header, "Lv.31 · —%", 12, FG, True, HEADER)
        self.level_text.pack(side="left", padx=(13, 0))
        self._drag_bind(header)
        self._drag_bind(self.level_text)
        self._icon_button(header, "닫기", self.close, color="#bac9d4").pack(side="right", padx=(0, 3))
        self._icon_button(header, "설정", self.show_settings, color="#bac9d4").pack(side="right")
        self._icon_button(header, "재검", self.rescan, color=ACCENT).pack(side="right")
        self.background_button = self._icon_button(
            header, "배경", self.toggle_background,
            color=ACCENT if self.config_data.get("transparent_bg", True) else MUTED)
        self.background_button.pack(side="right")
        self.pin_button = self._icon_button(header, "고정", self.toggle_pin,
                                            color=ACCENT if self.config_data.get("always_top", True) else MUTED)
        self.pin_button.pack(side="right")

        hero = tk.Frame(outer, bg=BG, height=85)
        hero.pack(fill="x", padx=15, pady=(9, 0))
        hero.pack_propagate(False)
        left = tk.Frame(hero, bg=BG)
        left.pack(side="left", fill="y")
        self.minute_heading = self._label(left, "1분당 경험치", 11, MUTED, bg=BG)
        self.minute_heading.pack(anchor="w")
        self.minute_text = self._label(left, "—", 34, ACCENT, True)
        self.minute_text.pack(anchor="w", pady=(0, 0))
        self._drag_bind(hero)
        right = tk.Frame(hero, bg=BG)
        right.pack(side="right", anchor="e", padx=(0, 3), pady=(3, 0))
        self.required_text = self._label(right, "총 필요 —", 11, MUTED)
        self.required_text.pack(anchor="e")
        self.remaining_text = self._label(right, "남은 경험치 —", 12, FG, True)
        self.remaining_text.pack(anchor="e")
        self.state_text = self._label(right, "인식 준비 중", 10, WARNING)
        self.state_text.pack(anchor="e")
        self._drag_bind(right)

        tk.Frame(outer, bg="#314150", height=1).pack(fill="x", padx=15, pady=(0, 8))
        metrics = tk.Frame(outer, bg=BG)
        metrics.pack(fill="x", padx=15)
        self.hour_text = self._label(metrics, "1시간 예상 —", 11, FG, True)
        self.hour_text.pack(side="left")
        self.gain_text = self._label(metrics, "누적 +0", 11, MUTED)
        self.gain_text.pack(side="right")
        self._drag_bind(metrics)

        haste_row = tk.Frame(outer, bg=HEADER, height=31)
        haste_row.pack(fill="x", padx=12, pady=(6, 0))
        haste_row.pack_propagate(False)
        self.haste_status_text = self._label(haste_row, "헤이스트 감시 준비 중", 11,
                                            MUTED, True, HEADER)
        self.haste_status_text.pack(side="left", padx=(9, 0))
        self.haste_sound_text = self._label(haste_row, "종료 알림 켜짐", 9,
                                           MUTED, bg=HEADER)
        self.haste_sound_text.pack(side="right", padx=(0, 9))
        self._drag_bind(haste_row)

        timed = tk.Frame(outer, bg=BG)
        timed.pack(fill="x", padx=12, pady=(8, 0))
        timed_header = tk.Frame(timed, bg=BG, height=32)
        timed_header.pack(fill="x", padx=3)
        timed_header.pack_propagate(False)
        self._label(timed_header, "시간별 누적 경험치", 11, FG, True).pack(side="left")
        self.new_session_button = tk.Button(
            timed_header, text="새 세션", command=self.start_new_session,
            bg="#315943", fg=FG, activebackground="#43805e",
            activeforeground=FG, relief="flat", bd=0, cursor="hand2",
            font=self._font(10, "bold"), padx=8, pady=3)
        self.new_session_button.pack(side="right")

        timer_grid = tk.Frame(timed, bg=BG)
        timer_grid.pack(fill="x", pady=(1, 0))
        timer_grid.grid_columnconfigure(0, weight=1, uniform="counter")
        timer_grid.grid_columnconfigure(1, weight=1, uniform="counter")
        self.timer_widgets = {}
        for index, (minutes, _) in enumerate(TIMERS):
            card = tk.Frame(timer_grid, bg=HEADER, height=74,
                            highlightbackground="#2b404f", highlightthickness=1)
            card.grid(row=index // 2, column=index % 2, sticky="ew", padx=3, pady=3)
            card.grid_propagate(False)
            first = tk.Frame(card, bg=HEADER)
            first.pack(fill="x", padx=9, pady=(6, 0))
            self._label(first, f"{LABELS[minutes]} 누적", 10, FG,
                        True, HEADER).pack(side="left")
            clock = self._label(first, "시작 대기", 9, MUTED, bg=HEADER)
            clock.pack(side="right")
            number = self._label(card, "인식 대기", 15, ACCENT, True, HEADER)
            number.pack(anchor="w", padx=9, pady=(0, 4))
            self.timer_widgets[minutes] = (number, clock)

        footer = tk.Frame(outer, bg=BG)
        footer.pack(fill="x", padx=15, pady=(6, 0))
        self.eta_text = self._label(footer, "레벨업 —", 10, MUTED)
        self.eta_text.pack(side="left")
        self.timer_text = self._label(footer, "00:00", 10, MUTED)
        self.timer_text.pack(side="right")
        self._drag_bind(footer)

    def _drag_start(self, event):
        self._drag_origin = (event.x_root - self.winfo_x(), event.y_root - self.winfo_y())

    def _drag_move(self, event):
        if self._drag_origin:
            x, y = self._drag_origin
            self.geometry(f"+{event.x_root - x}+{event.y_root - y}")

    def _drag_end(self, event):
        self._drag_origin = None
        self.config_data["overlay_geometry"] = [self.winfo_x(), self.winfo_y()]
        save_config(self.config_data)

    def toggle_pin(self):
        enabled = not bool(self.config_data.get("always_top", True))
        self.config_data["always_top"] = enabled
        self.attributes("-topmost", enabled)
        self.pin_button.configure(fg=ACCENT if enabled else MUTED)
        save_config(self.config_data)

    def toggle_background(self):
        """Quick-switch: completely clear HUD or classic translucent cards."""
        transparent = not bool(self.config_data.get("transparent_bg", True))
        self.config_data["transparent_bg"] = transparent
        self._apply_hud_transparency(transparent)
        self.background_button.configure(fg=ACCENT if transparent else MUTED)
        save_config(self.config_data)

    def start_new_session(self):
        if self.session and not self.session.stopped:
            self._archive_timer_session("new_session")
            self.store.finish(self.session)
        self.session = HuntSession(self.reading_level,
                                   str(self.config_data.get("map") or "사냥터 미지정"))
        self.store.begin(self.session)
        self.timers = ExpTimerSet()
        self.last_valid = None
        self.retry_count = 0
        self.rescan_event.set()
        self.scan_status = "경험치 자동 스캔 중"
        self._show_values()

    def _append_timer_log(self, record: dict):
        try:
            self.timer_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.timer_log_path.open("a", encoding="utf-8") as file:
                file.write(json.dumps({
                    "recorded": datetime.now().astimezone().isoformat(timespec="seconds"),
                    **record,
                }, ensure_ascii=False) + "\n")
        except OSError as error:
            self.ocr_error = f"타이머 기록 저장 실패: {error}"

    def _archive_timer_session(self, reason: str):
        hunt = self.session
        if hunt is None:
            return
        self._append_timer_log({
            "kind": "session_end", "reason": reason,
            "session_start": hunt.started.isoformat(timespec="seconds"),
            **self.timers.summary(hunt.elapsed(), hunt.db_id),
        })

    def pause_or_resume(self):
        if self.session is None or self.session.stopped:
            self.start_new_session()
        elif self.session.paused:
            self.session.resume()
            self.rescan_event.set()
        else:
            self.session.pause()
        self._show_values()

    def rescan(self):
        # Rescans never clear accumulated EXP or reset the timer.
        self.rescan_event.set()
        self.scan_status = "수동 재스캔 요청"
        self.diagnostics.write("manual_rescan")
        self._show_values()

    def test_haste_sound(self):
        threading.Thread(target=beep_haste_expired, daemon=True,
                         name="Maple-Haste-Alert-Test").start()

    def _show_haste_status(self):
        status = self.haste_status
        if (self.last_haste_sample_at is not None and
                time.monotonic() - self.last_haste_sample_at > 5.0):
            label, color = "헤이스트 화면 캡처 대기", WARNING
        elif status == "active":
            label, color = "헤이스트 적용 중", ACCENT
        elif status == "checking":
            label, color = "헤이스트 종료 확인 중", WARNING
        elif status == "expired":
            label, color = "헤이스트 종료 · 재사용하세요", ERROR
        elif status == "error":
            label, color = "헤이스트 감지 설정 오류", ERROR
        else:
            label, color = "헤이스트 감지 대기", MUTED
        self.haste_status_text.configure(text=label, fg=color)
        on = bool(self.config_data.get("haste_alert", True))
        self.haste_sound_text.configure(text="종료 알림 켜짐" if on else "종료 알림 꺼짐",
                                        fg=ACCENT if on else MUTED)

    def _open_menu(self, event=None):
        menu = tk.Menu(self, tearoff=0, bg=HEADER, fg=FG, activebackground="#334557",
                       activeforeground=FG, bd=1, font=self._font(10))
        menu.add_command(label="새 사냥 시작", command=self.start_new_session)
        menu.add_command(label="일시정지 / 재개", command=self.pause_or_resume)
        menu.add_command(label="지금 재스캔", command=self.rescan)
        menu.add_command(label="헤이스트 알림음 테스트", command=self.test_haste_sound)
        menu.add_separator()
        menu.add_command(label="EXP 스캔 영역 다시 지정", command=self.pick_exp_region)
        menu.add_command(label="설정", command=self.show_settings)
        menu.add_command(label="사냥 기록", command=self.show_history)
        menu.add_separator()
        menu.add_command(label="종료", command=self.close)
        try:
            menu.tk_popup(event.x_root if event else self.winfo_pointerx(),
                          event.y_root if event else self.winfo_pointery())
        finally:
            menu.grab_release()

    def _scan_worker(self):
        active_id = None
        last_frame = 0.0
        startup_candidate = None
        startup_hits = 0
        levelup_hits = 0
        last_valid_scan = None
        last_full_probe = 0.0
        last_rejection_notice = -1e9
        recovery = RecoveryPolicy()
        while not self.stop_event.is_set():
            began = time.monotonic()
            try:
                hunt = self.session
                if hunt is None or hunt.stopped:
                    self.stop_event.wait(0.25)
                    continue
                session_id = id(hunt)
                if session_id != active_id:
                    active_id = session_id
                    last_frame = 0.0
                    startup_candidate = None
                    startup_hits = 0
                    levelup_hits = 0
                    last_valid_scan = None
                    last_rejection_notice = -1e9
                    recovery = RecoveryPolicy()
                if self.rescan_event.is_set():
                    self.rescan_event.clear()
                    # Rescan the OCR, NOT the shared WGC source. Restarting
                    # capture on a font/OCR error interrupted Haste too.
                    last_frame = 0.0
                    self.event_queue.put(("status", "경험치 재인식 중"))
                info = game_info()
                if info is None:
                    self.event_queue.put(("status", "메이플 창 대기"))
                    self.stop_event.wait(0.7)
                    continue
                if not self.capture.ensure(info.hwnd):
                    self.event_queue.put(("status", "캡처 재연결 대기"))
                    self.stop_event.wait(0.7)
                    continue

                if hunt.paused:
                    self.stop_event.wait(0.25)
                    continue
                image, timestamp = self.capture.exp_snapshot()
                if image is None or timestamp <= last_frame:
                    self.stop_event.wait(0.1)
                    continue
                last_frame = timestamp
                current_level = self.reading_level
                result = read_exp(image, current_level, hunt.current_exp)
                # The original v1.5 automatic level repair is preserved.
                if result is None and hunt.current_exp is None:
                    guess = None
                    for level in (current_level + 1, current_level - 1,
                                  current_level + 2, current_level - 2):
                        if not 1 <= level <= 60:
                            continue
                        candidate = read_exp(image, level)
                        req = requirement(level)
                        if candidate and req and candidate[1] >= 2 and \
                                abs(candidate[0] / req * 100 - candidate[1]) < 0.2:
                            guess = (level, candidate)
                            break
                    if guess:
                        if startup_candidate == guess[0]:
                            startup_hits += 1
                        else:
                            startup_candidate, startup_hits = guess[0], 1
                        if startup_hits >= 2:
                            current_level, result = guess
                    else:
                        startup_hits = 0
                elif result is None and hunt.current_pct is not None and \
                        hunt.current_pct >= 68 and current_level < 60:
                    next_level = read_exp(image, current_level + 1)
                    if next_level:
                        levelup_hits += 1
                        if levelup_hits >= 2:
                            current_level, result = current_level + 1, next_level
                    else:
                        levelup_hits = 0
                if result is None and recovery.failures >= 1 and \
                        timestamp - last_full_probe >= 1.5:
                    # Retry on a slightly larger EXP crop: catches cut-off
                    # numbers after window/UI scale changes.
                    frame, frame_at = self.capture.full_snapshot()
                    if frame is not None and frame_at > last_full_probe:
                        last_full_probe = frame_at
                        x1, y1, x2, y2 = self.exp_roi
                        wide_roi = [max(0, x1 - .018), max(0, y1 - .009),
                                    min(1, x2 + .018), min(1, y2 + .009)]
                        wide = crop_game_region(frame, wide_roi)
                        if wide is not None:
                            result = read_exp(wide, current_level, hunt.current_exp)
                if result is not None:
                    reason = exp_rejection_reason(hunt, current_level, result[0])
                    if reason:
                        now = time.monotonic()
                        if now - last_rejection_notice >= 2:
                            self.event_queue.put(("exp_rejected", session_id, reason))
                            self.diagnostics.write("exp_ocr_rejected", reason=reason,
                                                   level=current_level, exp=result[0])
                            last_rejection_notice = now
                        result = None
                if result is not None:
                    recovery.success()
                    last_valid_scan = time.monotonic()
                    startup_hits = 0
                    levelup_hits = 0
                    self.event_queue.put(("exp", session_id, current_level,
                                          result[0], result[1], result[2]))
                else:
                    now = time.monotonic()
                    restart = recovery.failure(now, last_valid_scan,
                                                self.config_data.get("auto_rescan", True))
                    if restart:
                        # Keep the healthy shared image stream alive. The
                        # capture manager independently reconnects only if
                        # frames are genuinely stale for several seconds.
                        self.event_queue.put(("retry", "OCR 재시도 · 캡처 유지"))
                        self.diagnostics.write("exp_ocr_retry",
                                               last_exp=hunt.current_exp,
                                               capture_generation=self.capture.generation)
                    elif recovery.failures == 2:
                        self.event_queue.put(("status", "경험치 재인식 시도 중"))
            except Exception as error:
                self.event_queue.put(("error", str(error)))
            self.stop_event.wait(max(0.05, POLL_INTERVAL - (time.monotonic() - began)))

    def _haste_worker(self):
        """Independent HUD watcher so OCR delays do not postpone the beep."""
        last_buff_frame = 0.0
        last_error = ""
        consecutive_errors = 0
        error_notified = False
        last_generation = None
        capture_grace_until = 0.0
        while not self.stop_event.is_set():
            try:
                info = game_info()
                if info is not None and self.capture.ensure(info.hwnd):
                    generation = self.capture.generation
                    if generation != last_generation:
                        if last_generation is not None:
                            self.haste_monitor.suspend_capture()
                            self.diagnostics.write(
                                "haste_capture_reconnected", generation=generation)
                        last_generation = generation
                        last_buff_frame = 0.0
                        capture_grace_until = time.monotonic() + 1.0
                    buff, timestamp = self.capture.buff_snapshot()
                    # Never count missing/broken/cached captures as a vanished
                    # buff. Only a newer screenshot is meaningful evidence.
                    if (buff is not None and timestamp > last_buff_frame and
                            time.monotonic() - timestamp < 2.5):
                        last_buff_frame = timestamp
                        icon = self.haste_matcher.match(
                            buff, info.height,
                            last_position=(self.haste_monitor.last_position
                                           if self.haste_monitor.state in ("active", "checking")
                                           else None))
                        self.last_haste_sample_at = time.monotonic()
                        # A restarted WGC source can briefly deliver blank
                        # frames. Never interpret them as buff removal.
                        if not icon.visible and self.last_haste_sample_at < capture_grace_until:
                            continue
                        previous_state = self.haste_monitor.state
                        ended = self.haste_monitor.update(icon, now=timestamp)
                        status = self.haste_monitor.state
                        if consecutive_errors:
                            self.diagnostics.write(
                                "haste_worker_recovered", after_errors=consecutive_errors,
                                confidence=round(icon.score, 3))
                            self.event_queue.put(("haste", status, icon.score, False))
                            consecutive_errors = 0
                            error_notified = False
                        if status != previous_state or ended:
                            self.diagnostics.write(
                                "haste_transition", old=previous_state, new=status,
                                confidence=round(icon.score, 3),
                                position=icon.position,
                                generation=generation, alarm=ended)
                        # Show 'checking' only after multiple missing frames.
                        # A single bad frame must not flicker the overlay.
                        if ((status != previous_state and status != "checking") or
                                (status == "checking" and self.haste_monitor.missing_count == 3)
                                or ended):
                            self.event_queue.put(("haste", status, icon.score, ended))
                        last_error = ""
            except Exception as error:
                # Isolate the buff watcher from the original EXP OCR engine.
                consecutive_errors += 1
                if str(error) != last_error:
                    self.diagnostics.write("haste_worker_error", error=str(error))
                    last_error = str(error)
                # A single failed template comparison is not a broken Haste
                # feature. Mark an error only after sustained exceptions.
                if consecutive_errors >= 8 and not error_notified:
                    self.event_queue.put(("haste_error", str(error)))
                    error_notified = True
            # Poll fast enough to pick up the next ~200ms HUD capture without
            # an additional ~220ms watcher delay (v1.9.2 felt sluggish).
            self.stop_event.wait(0.045)

    def _tick(self):
        if self._closing:
            return
        try:
            changed = False
            for _ in range(80):
                try:
                    message = self.event_queue.get_nowait()
                except queue.Empty:
                    break
                kind = message[0]
                if kind == "exp" and self.session and not self.session.paused:
                    _, session_id, level, exp, pct, raw = message
                    if session_id != id(self.session):
                        continue
                    accepted = self.session.add_exp(level, exp, pct)
                    if accepted:
                        self.last_valid = time.monotonic()
                        self.retry_count = 0
                        self.last_sample_raw = raw
                        self.scan_status = "경험치 인식 정상"
                        if self.session.latest_total_exp is not None:
                            completed = self.timers.observe(
                                elapsed=self.session.elapsed(),
                                total_exp=self.session.latest_total_exp)
                            for result in completed:
                                self._append_timer_log({
                                    "kind": "checkpoint",
                                    "session_id": self.session.db_id,
                                    "session_start": self.session.started.isoformat(timespec="seconds"),
                                    **result.as_dict(),
                                })
                        if self.reading_level != level:
                            self.reading_level = level
                            self.config_data["level"] = level
                            save_config(self.config_data)
                        changed = True
                    else:
                        self.scan_status = "비정상 EXP 인식값 무시"
                        changed = True
                elif kind == "exp_rejected":
                    if self.session is not None and message[1] == id(self.session):
                        self.scan_status = message[2]
                        changed = True
                elif kind == "status":
                    self.scan_status = message[1]
                    changed = True
                elif kind == "retry":
                    self.scan_status = message[1]
                    self.retry_count += 1
                    changed = True
                elif kind == "haste":
                    _, self.haste_status, self.haste_confidence, ended = message
                    changed = True
                    if ended and self.config_data.get("haste_alert", True):
                        self.test_haste_sound()
                elif kind == "haste_error":
                    self.haste_status = "error"
                    self.haste_matcher_error = message[1]
                    changed = True
                elif kind == "error":
                    self.ocr_error = message[1]
                    self.scan_status = "OCR 오류 · 재시도 중"
                    self.diagnostics.write("exp_worker_error", error=message[1])
                    changed = True
            now = time.monotonic()
            if changed or now - self.last_redraw > 0.75:
                self._show_values()
                self.last_redraw = now
            if self.session and now - self.last_save >= 15:
                self.store.save(self.session)
                self.last_save = now
        except Exception:
            self.ocr_error = traceback.format_exc(limit=5)
        self.after(UI_INTERVAL_MS, self._tick)

    def _show_values(self):
        hunt = self.session
        self._show_haste_status()
        self.minute_heading.configure(text="1분당 경험치")
        if hunt is None:
            self.level_text.configure(text=f"Lv.{self.reading_level} · —%")
            self.minute_text.configure(text="—")
            needed, remaining = experience_progress(self.reading_level, None)
            self.required_text.configure(text="총 필요 " + fmt_int(needed))
            self.remaining_text.configure(text="남은 경험치 " + fmt_int(remaining))
            self.hour_text.configure(text="1시간 예상 —")
            self.gain_text.configure(text="누적 +0")
            self.eta_text.configure(text="레벨업 —")
            self.timer_text.configure(text="00:00")
            self.state_text.configure(text="준비 중", fg=WARNING)
            for minutes, _ in TIMERS:
                number, clock = self.timer_widgets[minutes]
                number.configure(text="인식 대기", fg=MUTED)
                clock.configure(text="시작 대기", fg=MUTED)
            return
        pct = hunt.current_pct
        self.level_text.configure(text=f"Lv.{hunt.level} · {pct:.2f}%" if pct is not None
                                  else f"Lv.{self.reading_level} · 인식 대기")
        needed, remaining = experience_progress(hunt.level, hunt.current_exp)
        self.required_text.configure(text="총 필요 " + fmt_int(needed))
        self.remaining_text.configure(text="남은 경험치 " + fmt_int(remaining))
        rate = hunt.rolling_rate("exp", 1)
        self.minute_text.configure(text=fmt_int(rate / 60 if rate is not None else None))
        self.hour_text.configure(text="1시간 예상 " + fmt_int(rate))
        self.gain_text.configure(text="누적 " + fmt_int(hunt.gained_exp, signed=True))
        for minutes, _ in TIMERS:
            value, subtitle, state = self.timers.display(minutes, hunt.elapsed())
            number, clock = self.timer_widgets[minutes]
            number.configure(text=value, fg=(ERROR if state == "missing_scan"
                                             else MUTED if state == "waiting" else ACCENT))
            clock.configure(text=subtitle, fg=(ACCENT if state == "complete"
                                                else ERROR if state == "missing_scan" else MUTED))
        self.eta_text.configure(text="레벨업 " + eta_text(hunt.until_level(rate)))
        elapsed = int(hunt.elapsed())
        self.timer_text.configure(text=f"{elapsed // 60:02}:{elapsed % 60:02}")
        age = time.monotonic() - self.last_valid if self.last_valid is not None else None
        if hunt.paused:
            state, color = "일시정지", MUTED
        elif age is not None and age < 4.0:
            state, color = "인식 정상", ACCENT
        elif self.scan_status in ("EXP 0 오인식 무시", "EXP 감소 오인식 무시"):
            state, color = "EXP 오인식 차단 중", WARNING
        elif age is not None and age >= 4.0:
            state, color = "재스캔 중", ERROR
        else:
            state, color = "스캔 중", WARNING
        self.state_text.configure(text=state, fg=color)

    def _save_region(self, roi):
        self.exp_roi = roi
        self.config_data["exp_roi"] = roi
        self.capture.set_exp_roi(roi)
        save_config(self.config_data)
        self.rescan()

    def pick_exp_region(self):
        info = game_info()
        if info is None:
            messagebox.showwarning("게임 감지 실패", "메이플 창이 켜져 있는지 확인해 주세요.", parent=self)
            return
        if self._picker_window and self._picker_window.winfo_exists():
            self._picker_window.lift()
            return
        # Hiding this overlay prevents its pixels from contaminating the crop.
        self.withdraw()

        def capture():
            if self._closing:
                return
            try:
                snapshot, _ = self.capture.full_snapshot()
                if snapshot is None:
                    snapshot = grab_game(info)
            except Exception as error:
                self.deiconify()
                messagebox.showerror("캡처 실패", str(error), parent=self)
                return
            self.deiconify()
            win = ExpRegionPicker(self, snapshot, self.exp_roi, self._save_region)
            self._picker_window = win
            def reset_picker(event):
                if event.widget is win:
                    self._picker_window = None
            win.bind("<Destroy>", reset_picker, add="+")

        self.after(350, capture)

    def show_settings(self):
        if self._settings_window and self._settings_window.winfo_exists():
            self._settings_window.lift()
            return
        win = tk.Toplevel(self)
        self._settings_window = win
        win.title("트래커 설정")
        win.configure(bg=BG)
        win.geometry("375x475")
        win.resizable(False, False)
        win.attributes("-topmost", True)
        self._label(win, "오버레이 설정", 15, FG, True).pack(anchor="w", padx=18, pady=(14, 8))
        self._label(win, "캐릭터 레벨", 10, MUTED).pack(anchor="w", padx=18)
        level = tk.Entry(win, bg=HEADER, fg=FG, insertbackground=FG,
                         highlightthickness=0, relief="flat", font=self._font(13))
        level.insert(0, str(self.reading_level))
        level.pack(fill="x", padx=18, pady=(3, 9), ipady=5)
        transparent = tk.BooleanVar(value=bool(self.config_data.get("transparent_bg", True)))
        opacity = tk.DoubleVar(value=clamp_alpha(self.config_data.get("overlay_alpha", .55)))
        def sync_background():
            self._apply_hud_transparency(transparent.get(), opacity.get())
            transparency.configure(state="disabled" if transparent.get() else "normal")

        tk.Checkbutton(win, text="배경 완전 투명 (글자·숫자 100% 선명)",
                       variable=transparent, command=sync_background,
                       bg=BG, fg=ACCENT, activebackground=BG,
                       activeforeground=ACCENT, selectcolor=HEADER,
                       font=self._font(11, "bold")).pack(anchor="w", padx=16, pady=(3, 6))
        self._label(win, "기존 배경 불투명도 (완전 투명 해제 시)", 10, MUTED).pack(
            anchor="w", padx=18)
        transparency = tk.Scale(win, from_=0.55, to=1.0, resolution=.05,
                                orient="horizontal", variable=opacity,
                                command=lambda x: self._apply_hud_transparency(
                                    False, float(x)) if not transparent.get() else None,
                                bg=BG, fg=MUTED, highlightthickness=0, troughcolor=HEADER,
                                length=280, showvalue=False)
        transparency.pack(padx=18, fill="x")
        transparency.configure(state="disabled" if transparent.get() else "normal")
        top = tk.BooleanVar(value=bool(self.config_data.get("always_top", True)))
        retry = tk.BooleanVar(value=bool(self.config_data.get("auto_rescan", True)))
        haste_alert = tk.BooleanVar(value=bool(self.config_data.get("haste_alert", True)))
        tk.Checkbutton(win, text="항상 위 고정 (게임 위 오버레이)", variable=top,
                       bg=BG, fg=FG, activebackground=BG, activeforeground=FG,
                       selectcolor=HEADER, font=self._font(10)).pack(anchor="w", padx=16)
        tk.Checkbutton(win, text="인식 실패 시 자동 재스캔 및 재연결", variable=retry,
                       bg=BG, fg=FG, activebackground=BG, activeforeground=FG,
                       selectcolor=HEADER, font=self._font(10)).pack(anchor="w", padx=16)
        tk.Checkbutton(win, text="헤이스트 종료 시 삐빅 알림", variable=haste_alert,
                       bg=BG, fg=FG, activebackground=BG, activeforeground=FG,
                       selectcolor=HEADER, font=self._font(10)).pack(anchor="w", padx=16)

        def commit():
            try:
                new_level = int(level.get())
                if not 1 <= new_level <= 60:
                    raise ValueError()
            except ValueError:
                messagebox.showwarning("레벨 오류", "레벨은 1~60 중 숫자로 입력해 주세요.", parent=win)
                return
            if self.session and self.session.current_exp is not None and new_level != self.reading_level:
                if not messagebox.askyesno("레벨 변경", "레벨을 바꾸면 새 세션이 시작됩니다. 계속할까요?", parent=win):
                    return
            level_changed = new_level != self.reading_level
            self.reading_level = new_level
            self.config_data.update(level=new_level, overlay_alpha=clamp_alpha(opacity.get()),
                                    transparent_bg=transparent.get(),
                                    always_top=top.get(), auto_rescan=retry.get(),
                                    haste_alert=haste_alert.get())
            self._apply_hud_transparency(transparent.get(), opacity.get())
            self.attributes("-topmost", top.get())
            self.pin_button.configure(fg=ACCENT if top.get() else MUTED)
            self.background_button.configure(fg=ACCENT if transparent.get() else MUTED)
            save_config(self.config_data)
            self._show_haste_status()
            win.destroy()
            if level_changed:
                self.start_new_session()

        tk.Button(win, text="EXP 위치 다시 지정", command=self.pick_exp_region,
                  bg=HEADER, fg=FG, activebackground="#334557", relief="flat",
                  font=self._font(10)).pack(fill="x", padx=18, pady=(5, 5), ipady=5)
        tk.Button(win, text="헤이스트 삐빅 소리 테스트", command=self.test_haste_sound,
                  bg=HEADER, fg=FG, activebackground="#334557", relief="flat",
                  font=self._font(10)).pack(fill="x", padx=18, pady=(0, 5), ipady=5)
        tk.Button(win, text="저장", command=commit,
                  bg="#44745b", fg=FG, activebackground="#56906f", relief="flat",
                  font=self._font(10, "bold")).pack(fill="x", padx=18, ipady=5)
        def cancel():
            # Background previews are reversible without saving settings.
            self._apply_hud_transparency(bool(self.config_data.get("transparent_bg", True)))
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", cancel)

    def show_history(self):
        win = tk.Toplevel(self)
        win.title("사냥 및 누적 타이머 기록")
        win.configure(bg=BG)
        win.geometry("430x380")
        win.attributes("-topmost", True)
        tk.Label(win, text="누적 경험치 타이머 기록", bg=BG, fg=FG,
                 font=self._font(14, "bold")).pack(anchor="w", padx=14, pady=10)
        listing = tk.Text(win, height=13, bg=HEADER, fg=FG, font=self._font(10),
                          relief="flat", highlightthickness=0, wrap="word")
        listing.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        if self.timer_log_path.exists():
            try:
                saved = [json.loads(line) for line in
                         self.timer_log_path.read_text(encoding="utf-8").splitlines()
                         if line.strip()]
            except (ValueError, OSError) as error:
                saved = []
                self.ocr_error = f"타이머 기록 열기 실패: {error}"
            for record in reversed(saved[-80:]):
                if record.get("kind") != "checkpoint":
                    continue
                label = LABELS.get(record.get("minutes"), "측정")
                gain = record.get("exp")
                text = (f"{gain:+,} EXP" if gain is not None else "OCR 누락")
                listing.insert("end", f"{record.get('recorded', '')[5:16]}  "
                                      f"{label} 누적  {text}\n")
        listing.insert("end", "\n최근 전체 세션\n")
        rows = self.store.recent()
        for row in rows[:10]:
            listing.insert("end", f"{row['started'][:16]}  Lv.{row['level_start']} "
                           f"+{row['gained_exp']:,} EXP\n")
        if not rows:
            listing.insert("end", "아직 기록이 없습니다.")
        listing.configure(state="disabled")

    def close(self):
        if self._closing:
            return
        self._closing = True
        self.stop_event.set()
        try:
            self.capture.stop()
        except Exception:
            pass
        if self.session and not self.session.stopped:
            self._archive_timer_session("close")
            self.store.finish(self.session)
        self.store.close()
        self.config_data["overlay_geometry"] = [self.winfo_x(), self.winfo_y()]
        save_config(self.config_data)
        self.destroy()


def self_test():
    assert parse_exp_text("EXP 44,607[41.12%]") == (44607, 41.12)
    assert requirement(31) == 108480
    assert experience_progress(31, 44607) == (108480, 63873)
    assert experience_progress(31, None) == (108480, None)
    assert experience_progress(31, 108480) == (108480, 0)
    assert experience_progress(31, 108481) == (108480, None)
    assert [duration for duration, _ in TIMERS] == [10, 30, 6, 60]
    assert HasteMatcher().template_path.is_file()
    assert clamp_alpha(0.1) == 0.55
    assert clamp_alpha(10) == 1.0
    r = RecoveryPolicy()
    assert not r.failure(1.0, None)
    assert not r.failure(1.5, None)
    assert r.failure(2.0, None)
    assert not r.failure(3.0, None)
    assert not r.failure(3.5, None)
    assert not r.failure(4.0, None)
    assert r.failure(9.5, None)
    r.success()
    assert not r.failure(10.0, 9.9)
    print("OVERLAY_SELF_TEST_OK", VERSION)


def ui_test():
    global BASE, CONFIG_PATH
    with tempfile.TemporaryDirectory(prefix="maple-overlay-ui-") as temp:
        BASE = Path(temp)
        CONFIG_PATH = BASE / "settings.json"
        app = OverlayApp(start_worker=False, start_session=True)
        app.session.add_exp(31, 44607, 41.12)
        app.timers.observe(elapsed=app.session.elapsed(),
                           total_exp=app.session.latest_total_exp)
        app.session.start_clock -= 30
        app.session.add_exp(31, 45007, 41.49)
        app.timers.observe(elapsed=app.session.elapsed(),
                           total_exp=app.session.latest_total_exp)
        app.last_valid = time.monotonic()
        app._show_values()
        assert app.minute_text.cget("text") == "800", app.minute_text.cget("text")
        assert app.level_text.cget("text") == "Lv.31 · 41.49%"
        assert app.required_text.cget("text") == "총 필요 108,480"
        assert app.remaining_text.cget("text") == "남은 경험치 63,473"
        assert app.hour_text.cget("text").startswith("1시간 예상 ")
        assert "시급" not in app.hour_text.cget("text")
        assert not hasattr(app, "period_buttons")
        assert [minutes for minutes in app.timer_widgets] == [10, 30, 6, 60]
        for minutes, _ in TIMERS:
            number, clock = app.timer_widgets[minutes]
            assert number.cget("text") == "+400 EXP"
            assert clock.cget("text").count(":") == 1
        assert app.session.gained_exp == 400
        old_hunt = app.session
        app.new_session_button.invoke()
        assert old_hunt.stopped
        assert app.session is not old_hunt
        assert app.session.gained_exp == 0
        assert app.session.current_exp is None
        assert app.timers.anchor_total is None
        assert app.minute_text.cget("text") == "—"
        assert app.timer_text.cget("text") == "00:00"
        assert app.store.recent()[1]["ended"] is not None
        assert app.timer_log_path.exists()
        # The two-tone alert fires only after an explicit expiry event and
        # respects the opt-out, without playing a real sound during QA.
        beep_calls = []
        app.test_haste_sound = lambda: beep_calls.append("beep")
        app.event_queue.put(("haste", "active", 0.96, False))
        app._tick()
        assert app.haste_status_text.cget("text") == "헤이스트 적용 중"
        assert not beep_calls
        app.event_queue.put(("haste", "checking", 0.397, False))
        app._tick()
        assert app.haste_status_text.cget("text") == "헤이스트 종료 확인 중"
        assert not beep_calls
        app.event_queue.put(("haste", "expired", 0.41, True))
        app._tick()
        assert len(beep_calls) == 1
        assert "헤이스트 종료" in app.haste_status_text.cget("text")
        app.config_data["haste_alert"] = False
        app.event_queue.put(("haste", "expired", 0.40, True))
        app._tick()
        assert len(beep_calls) == 1
        assert app.haste_sound_text.cget("text") == "종료 알림 꺼짐"
        assert app.attributes("-topmost") == 1
        # Also exercise the packaged high-resolution EXP picker UI. The
        # original full-screen thumbnail made EXP text unreadably small.
        picker = ExpRegionPicker(app, Image.new("RGB", (3840, 2160), "#172231"),
                                 list(DEFAULT_EXP_REGION), lambda roi: None)
        app.update()
        picker.render()
        assert picker.view is not None
        assert picker.view.zoom >= 1.5
        assert picker.image_ref is not None
        assert picker.image_ref.width() == picker.canvas.winfo_width()
        picker.destroy()
        # Tk waits until mapped to lay out overrideredirect children.
        app.update()
        app.update_idletasks()
        assert int(app.winfo_width()) == WIDTH
        assert int(app.winfo_height()) == HEIGHT
        for control in (app.level_text, app.minute_text, app.required_text,
                        app.remaining_text, app.hour_text, app.gain_text,
                        app.haste_status_text, app.haste_sound_text,
                        app.eta_text, app.timer_text, app.new_session_button,
                        *(item for pair in app.timer_widgets.values() for item in pair)):
            assert control.winfo_rootx() >= app.winfo_rootx(), control.cget("text")
            assert (control.winfo_rootx() + control.winfo_width() <=
                    app.winfo_rootx() + WIDTH), control.cget("text")
            assert (control.winfo_rooty() + control.winfo_height() <=
                    app.winfo_rooty() + HEIGHT), control.cget("text")
        app.after(1500, app.close)
        print("OVERLAY_UI_TEST_OK", "FOUR_TIMERS_NEW_SESSION")
        app.mainloop()


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
    elif "--ui-test" in sys.argv:
        ui_test()
    else:
        BASE.mkdir(parents=True, exist_ok=True)
        OverlayApp().mainloop()
