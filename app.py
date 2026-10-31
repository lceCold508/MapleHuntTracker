"""MapleHuntTracker standalone GUI. Screen OCR only; no game process access."""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import traceback
import tkinter as tk
import ctypes
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk
from PIL import Image, ImageTk

from engine import HuntSession, SessionStore, eta_text, fmt_hms, fmt_int, requirement
from meso_detector import AutoMesoDetector
from window_capture import GameWindowCapture
from screen import (
    DEFAULT_EXP_REGION, capture_region, crop_game_region, game_info, grab_game,
    ocr_available, read_exp,
)

VERSION = "1.5.0"
APP_NAME = "글클메 사냥 트래커"
# EXP is a tiny, independent OCR scan. Full-screen MESOS scanning must never
# block it. The intervals are start-to-start targets, not a fixed sleep after
# OCR, so recognition time does not accumulate into additional lag.
EXP_POLL_INTERVAL = 0.50
MESO_POLL_INTERVAL = 3.5
UI_TICK_MS = 160
BASE = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "MapleHuntTracker"
CONFIG_PATH = BASE / "settings.json"
PURPLE = "#7265fc"
LAVENDER = "#ad8afe"
GREEN = "#27d69c"
BG = "#0c0f15"
CARD = "#151a24"
PANEL = "#1d2330"
MUTED = "#8e9ab2"
WHITE = "#f2f4fa"
RED = "#f1748f"

ctk.set_default_color_theme("blue")
ctk.set_appearance_mode("dark")


def load_config() -> dict:
    defaults = {
        "level": 26,
        "map": "사냥터 미지정",
        "exp_roi": DEFAULT_EXP_REGION,
        "meso_roi": None,
        "always_top": True,
        "period": 5,
    }
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            defaults.update(data)
    except (OSError, ValueError):
        pass
    return defaults


def save_config(data: dict) -> None:
    BASE.mkdir(parents=True, exist_ok=True)
    temp = CONFIG_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(CONFIG_PATH)


def label(parent, text, size=12, color=WHITE, bold=False, **kw):
    return ctk.CTkLabel(parent, text=text, font=ctk.CTkFont(
        family="Malgun Gothic", size=size, weight="bold" if bold else "normal"),
        text_color=color, **kw)


def button(parent, text, command, width=70, height=31, fg=PANEL, hover="#2b3445",
           color=WHITE, **kw):
    return ctk.CTkButton(
        parent, text=text, command=command, width=width, height=height,
        corner_radius=10, fg_color=fg, hover_color=hover, text_color=color,
        font=ctk.CTkFont(family="Malgun Gothic", size=12, weight="bold"), **kw)


class Sparkline(tk.Canvas):
    def __init__(self, parent, color=PURPLE):
        super().__init__(parent, height=54, background=CARD,
                         highlightthickness=0, relief="flat")
        self.color = color
        self.data = [0] * 12
        self.bind("<Configure>", lambda _: self.render())

    def update_values(self, values):
        self.data = values
        self.render()

    def render(self):
        self.delete("all")
        width = max(self.winfo_width(), 50)
        height = max(self.winfo_height(), 30)
        self.create_line(0, height - 3, width, height - 3, fill="#28303e", width=1)
        data = self.data
        vmax = max(1, max(abs(float(n)) for n in data))
        bar_width = width / max(1, len(data))
        center = height * 0.78
        for i, value in enumerate(data):
            x1 = i * bar_width + 3
            x2 = (i + 1) * bar_width - 3
            amt = min(height * 0.7, abs(value) / vmax * height * 0.63)
            if value < 0:
                y1, y2 = center, min(height - 2, center + amt)
                fill = RED
            else:
                y1, y2 = center - amt, center
                fill = self.color
            if x1 < x2:
                self.create_rectangle(x1, y1, x2, y2, fill=fill, outline="")


class NumberCard(ctk.CTkFrame):
    def __init__(self, parent, heading, accent):
        super().__init__(parent, fg_color=PANEL, corner_radius=11, height=93)
        self.pack_propagate(False)
        label(self, heading, 11, MUTED).pack(anchor="w", padx=12, pady=(10, 1))
        self.value = label(self, "—", 23, accent, True)
        self.value.pack(anchor="w", padx=12, pady=(1, 0))
        self.sub = label(self, "수치 읽는 중", 10, MUTED)
        self.sub.pack(anchor="w", padx=12)


class Picker(ctk.CTkToplevel):
    def __init__(self, parent, image: Image.Image, title: str, callback):
        super().__init__(parent)
        self.title(title)
        self.attributes("-topmost", True)
        self.configure(fg_color=BG)
        self.callback = callback
        self.orig_size = image.size
        sw = max(700, min(parent.winfo_screenwidth() - 100, 1440))
        sh = max(450, min(parent.winfo_screenheight() - 190, 850))
        scale = min(sw / image.width, sh / image.height)
        self.display = image.resize((int(image.width * scale), int(image.height * scale)),
                                    Image.Resampling.BILINEAR)
        self.photo = ImageTk.PhotoImage(self.display)
        self.geometry(f"{self.display.width + 24}x{self.display.height + 89}")
        label(self, "드래그해서 숫자와 주변 글자만 감싸세요.  •  ESC 취소", 13,
              WHITE, True).pack(pady=8)
        self.canvas = tk.Canvas(self, width=self.display.width, height=self.display.height,
                                bg="#151515", borderwidth=0, highlightthickness=0)
        self.canvas.pack()
        self.canvas.create_image(0, 0, image=self.photo, anchor="nw")
        self.start = None
        self.rectangle = None
        self.canvas.bind("<ButtonPress-1>", self.on_start)
        self.canvas.bind("<B1-Motion>", self.on_move)
        self.canvas.bind("<ButtonRelease-1>", self.on_end)
        self.bind("<Escape>", lambda _: self.destroy())
        self.grab_set()
        self.focus_force()

    def on_start(self, event):
        self.start = (event.x, event.y)
        if self.rectangle:
            self.canvas.delete(self.rectangle)
        self.rectangle = self.canvas.create_rectangle(
            event.x, event.y, event.x, event.y, outline="#d7aaff", width=3)

    def on_move(self, event):
        if self.start and self.rectangle:
            self.canvas.coords(self.rectangle, *self.start, event.x, event.y)

    def on_end(self, event):
        if not self.start:
            return
        x1, x2 = sorted((self.start[0], event.x))
        y1, y2 = sorted((self.start[1], event.y))
        if x2 - x1 < 16 or y2 - y1 < 10:
            return
        bounds = [
            max(0.0, x1 / self.display.width), max(0.0, y1 / self.display.height),
            min(1.0, x2 / self.display.width), min(1.0, y2 / self.display.height)]
        self.callback(bounds)
        self.destroy()


class TrackerApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME + " · " + VERSION)
        vx = ctypes.windll.user32.GetSystemMetrics(76)
        vy = ctypes.windll.user32.GetSystemMetrics(77)
        vw = ctypes.windll.user32.GetSystemMetrics(78)
        vh = ctypes.windll.user32.GetSystemMetrics(79)
        # Stay comfortably inside the secondary display even on mixed-DPI
        # monitors (Tk window positions can be virtualized by Windows).
        self.geometry(f"434x851+{vx + max(0, vw - 850)}+{vy + 54}")
        self.minsize(410, 710)
        self.configure(fg_color=BG)
        self.config = load_config()
        saved_geometry = self.config.get("geometry")
        if isinstance(saved_geometry, list) and len(saved_geometry) == 2:
            try:
                x, y = [int(v) for v in saved_geometry]
                if vx <= x <= vx + vw - 200 and vy <= y <= vy + vh - 200:
                    self.geometry(f"434x851+{x}+{y}")
            except (TypeError, ValueError):
                pass
        self.attributes("-topmost", bool(self.config["always_top"]))
        self.store = SessionStore(BASE)
        self.session: HuntSession | None = None
        self.finished_sample_index = -1
        self.last_saved = 0.0
        self.capture_stop = threading.Event()
        self.capture_messages: queue.SimpleQueue = queue.SimpleQueue()
        self.last_render_time = 0.0
        self.last_valid_exp_at: float | None = None
        self.valid_exp_reads = 0
        self.reading_level = int(self.config.get("level", 26))
        self.last_meso_seen: int | None = None
        self.meso_pending: int | None = None
        self.meso_pending_time = 0.0
        self.meso_pending_count = 0
        self.last_meso_visible_at: float | None = None
        self.ocr_error = ""
        self.last_ocr_raw = ""
        self.current_area = game_info()
        self.ocr_supported = ocr_available()
        self.period = ctk.IntVar(value=int(self.config.get("period", 5)))
        self.exp_roi = self.config.get("exp_roi", DEFAULT_EXP_REGION)
        self.window_capture = GameWindowCapture(self.exp_roi)
        self.ready = False
        self.protocol("WM_DELETE_WINDOW", self.quit_app)
        self._render_shell()
        self._render_values()
        self.ready = True
        self.worker = threading.Thread(target=self._capture_worker, daemon=True,
                                       name="Maple-EXP-OCR")
        self.worker.start()
        self.meso_worker = threading.Thread(target=self._meso_worker, daemon=True,
                                            name="Maple-MESO-OCR")
        self.meso_worker.start()
        self.after(UI_TICK_MS, self._ui_tick)

    def _render_shell(self):
        # Top bar
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=14, pady=(14, 5))
        left = ctk.CTkFrame(top, fg_color="transparent")
        left.pack(side="left")
        label(left, "사냥 트래커", 22, WHITE, True).pack(anchor="w")
        self.status = label(left, "● 준비", 11, MUTED)
        self.status.pack(anchor="w")
        right = ctk.CTkFrame(top, fg_color="transparent")
        right.pack(side="right", anchor="n")
        self.clock_label = label(right, "00:00:00", 21, WHITE, True)
        self.clock_label.pack(side="left", padx=(0, 8))
        self.play_button = button(right, "▶", self.toggle_session,
                                  width=53, height=42, fg=PURPLE, hover="#5b52d7")
        self.play_button.pack(side="right")
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.pack(fill="x", padx=10, pady=(3, 8))
        for text, method, width in [
            ("새 세션", self.new_session, 72),
            ("기록", self.show_history, 49),
            ("설정", self.show_settings, 50),
        ]:
            button(actions, text, method, width=width, height=31).pack(side="left", padx=2)
        self.meso_auto_badge = label(actions, "● 메소 자동 감지", 12, GREEN, True)
        self.meso_auto_badge.pack(side="right", padx=(4, 8))
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=14, pady=(1, 1))
        self.level_label = label(header, "Lv. 26", 12, LAVENDER, True)
        self.level_label.pack(side="left")
        self.map_label = label(header, "사냥터 미지정", 11, MUTED)
        self.map_label.pack(side="right")
        period_title = label(self, "경험치 예측 기준 · 메소는 누적 사냥시간 평균", 10, MUTED)
        period_title.pack(anchor="w", padx=14, pady=(4, 3))
        tabs = ctk.CTkFrame(self, fg_color="transparent")
        tabs.pack(fill="x", padx=10, pady=(0, 9))
        self.period_buttons = {}
        for mins in (1, 5, 10, 15, 30, 60):
            btn = button(tabs, f"{mins}분", lambda value=mins: self.set_period(value),
                         width=55, height=26)
            btn.pack(side="left", padx=3, expand=True, fill="x")
            self.period_buttons[mins] = btn
        self.notice = ctk.CTkFrame(self, fg_color="#332707", corner_radius=11)
        self.notice.pack(fill="x", padx=12, pady=(0, 10))
        self.notice_text = label(
            self.notice, "메이플 창 직접 캡처  •  다른 창에 가려져도 경험치 추적", 11, "#f4c552")
        self.notice_text.pack(fill="x", padx=11, pady=10)
        # EXP
        exp = ctk.CTkFrame(self, fg_color=CARD, border_width=1,
                           border_color="#29303f", corner_radius=16)
        exp.pack(fill="x", padx=12, pady=(0, 9))
        exp_title = ctk.CTkFrame(exp, fg_color="transparent")
        exp_title.pack(fill="x", padx=15, pady=(11, 6))
        label(exp_title, "경험치", 15, WHITE, True).pack(side="left")
        self.exp_pct_label = label(exp_title, "—", 12, LAVENDER, True)
        self.exp_pct_label.pack(side="right")
        exp_cards = ctk.CTkFrame(exp, fg_color="transparent")
        exp_cards.pack(fill="x", padx=11, pady=(1, 4))
        self.exp_minute = NumberCard(exp_cards, "1분당", LAVENDER)
        self.exp_minute.pack(side="left", fill="both", expand=True, padx=(0, 5))
        self.exp_hour = NumberCard(exp_cards, "1시간 예상", WHITE)
        self.exp_hour.pack(side="right", fill="both", expand=True, padx=(5, 0))
        self.exp_detail = label(exp, "누적 +0 EXP  ·  현재 EXP 인식 대기", 11, MUTED)
        self.exp_detail.pack(anchor="w", padx=15, pady=(7, 5))
        self.exp_bar = ctk.CTkProgressBar(exp, height=8, progress_color=LAVENDER,
                                          fg_color=PANEL, corner_radius=6)
        self.exp_bar.pack(fill="x", padx=15, pady=(0, 4))
        self.exp_bar.set(0)
        self.exp_eta = label(exp, "레벨업 예상 · 데이터 수집 중", 10, MUTED)
        self.exp_eta.pack(anchor="e", padx=15, pady=(0, 5))
        self.exp_chart = Sparkline(exp, LAVENDER)
        self.exp_chart.pack(fill="x", padx=12, pady=(1, 5))
        label(exp, "최근 사냥 경험치 추세 (1분당)", 10, MUTED).pack(
            anchor="w", padx=15, pady=(0, 10))
        # MESO
        meso = ctk.CTkFrame(self, fg_color=CARD, border_width=1,
                            border_color="#29303f", corner_radius=16)
        meso.pack(fill="x", padx=12, pady=(0, 7))
        meso_title = ctk.CTkFrame(meso, fg_color="transparent")
        meso_title.pack(fill="x", padx=15, pady=(11, 6))
        label(meso_title, "메소", 15, WHITE, True).pack(side="left")
        self.meso_gain_label = label(meso_title, "누적 +0", 12, GREEN, True)
        self.meso_gain_label.pack(side="right")
        meso_cards = ctk.CTkFrame(meso, fg_color="transparent")
        meso_cards.pack(fill="x", padx=11, pady=(0, 4))
        self.meso_minute = NumberCard(meso_cards, "1분 평균 순증감", GREEN)
        self.meso_minute.pack(side="left", expand=True, fill="both", padx=(0, 5))
        self.meso_hour = NumberCard(meso_cards, "1시간 평균 예상", GREEN)
        self.meso_hour.pack(side="right", expand=True, fill="both", padx=(5, 0))
        # Keep the session starting balance visible alongside the latest
        # balance; the green headline above is the net difference.
        meso_balances = ctk.CTkFrame(meso, fg_color="transparent")
        meso_balances.pack(fill="x", padx=15, pady=(7, 6))
        self.meso_baseline_label = label(meso_balances, "기준 메소: —", 11, MUTED)
        self.meso_baseline_label.pack(side="left")
        self.meso_detail = label(meso_balances, "현재 메소: —", 11, WHITE)
        self.meso_detail.pack(side="right")
        self.meso_chart = Sparkline(meso, GREEN)
        self.meso_chart.pack(fill="x", padx=12, pady=(0, 4))
        label(meso, "기준~마지막 잔액 확인 사이의 누적 평균 추세", 10, MUTED).pack(
            anchor="w", padx=15, pady=(0, 10))
        self.footer = label(self, "게임 화면만 캡처 · 메모리/패킷 접근 없음", 10, MUTED)
        self.footer.pack(side="bottom", pady=(1, 8))
        self.set_period(self.period.get())

    def set_period(self, minutes):
        self.period.set(minutes)
        self.config["period"] = minutes
        for period, control in self.period_buttons.items():
            control.configure(fg_color=PURPLE if period == minutes else PANEL,
                              hover_color="#5e53d8" if period == minutes else "#293344")
        save_config(self.config)
        if self.ready:
            self._render_values()

    def toggle_session(self):
        if not self.session or self.session.stopped:
            self.new_session()
            return
        if self.session.paused:
            self.session.resume()
            self.play_button.configure(text="Ⅱ")
        else:
            self.session.pause()
            self.play_button.configure(text="▶")
        self.store.save(self.session)
        self._render_values()

    def new_session(self):
        if self.session and not self.session.stopped:
            self.store.finish(self.session)
        try:
            level = int(self.config["level"])
        except (ValueError, TypeError):
            level = 26
        self.session = HuntSession(level, str(self.config.get("map") or "사냥터 미지정"))
        self.reading_level = level
        self.store.begin(self.session)
        self.play_button.configure(text="Ⅱ")
        self.last_meso_seen = None
        self.meso_pending = None
        self.meso_pending_count = 0
        self.last_meso_visible_at = None
        self.last_valid_exp_at = None
        self.valid_exp_reads = 0
        self._render_values()

    def manual_meso(self):
        dialog = ctk.CTkInputDialog(
            text="지금 인벤토리에 표시된 메소 잔액을 입력해 주세요.\n"
                 "이후 잔액을 다시 입력하면 차액이 순수익으로 집계됩니다.",
            title="현재 메소 잔액 입력")
        answer = dialog.get_input()
        if not answer:
            return
        try:
            value = int(answer.replace(",", "").replace(" ", ""))
            if not 0 <= value <= 9_999_999_999:
                raise ValueError()
        except ValueError:
            messagebox.showwarning("입력 오류", "메소 잔액을 숫자로 입력해 주세요.")
            return
        if not self.session:
            self.new_session()
        if self.session.paused:
            messagebox.showinfo("일시정지", "▶ 버튼을 눌러 재개한 후 메소를 입력해 주세요.")
            return
        self.session.add_meso(value)
        self.store.save(self.session)
        self._render_values()

    def _capture_worker(self):
        retry_level = 0
        startup_level_candidate = None
        startup_level_hits = 0
        active_session_id = None
        last_frame_time = 0.0
        while not self.capture_stop.is_set():
            started = time.monotonic()
            try:
                hunt = self.session
                if hunt is None or hunt.paused or hunt.stopped:
                    self.capture_stop.wait(0.30)
                    continue
                session_id = id(hunt)
                if session_id != active_session_id:
                    active_session_id = session_id
                    retry_level = 0
                    startup_level_candidate = None
                    startup_level_hits = 0
                    last_frame_time = 0
                info = game_info()
                if info is None:
                    self.capture_messages.put(("status", "게임 창을 찾을 수 없음"))
                    self.capture_stop.wait(0.75)
                    continue
                # Capture the real MapleStory window even when another
                # fullscreen game, browser or the tracker overlaps it.
                # ImageGrab of desktop coordinates previously read another
                # app's pixels and left the EXP total stuck at zero.
                direct = getattr(self, "window_capture", None)
                if direct is not None:
                    if not direct.ensure(info.hwnd):
                        self.capture_messages.put(
                            ("status", "메이플 창 직접 캡처 실패 · " + direct.error[:50]))
                        self.capture_stop.wait(0.7)
                        continue
                    xp_area, frame_time = direct.exp_snapshot()
                    if xp_area is None:
                        self.capture_messages.put(("status", "메이플 창 영상 대기 중"))
                        self.capture_stop.wait(0.25)
                        continue
                    if frame_time <= last_frame_time:
                        self.capture_stop.wait(0.10)
                        continue
                    last_frame_time = frame_time
                else:
                    # Test-only compatibility path for deterministic worker
                    # tests that pass a minimal fake tracker object.
                    xp_area = capture_region(info, self.exp_roi)
                if xp_area:
                    result = read_exp(xp_area, self.reading_level, hunt.current_exp)
                    # If the stored level differs from the current game level
                    # at session start, EXP OCR will reject every reading.
                    # Probe adjacent levels only on initial failure and
                    # require TWO matching scans before changing the setting.
                    if result is None and hunt.current_exp is None:
                        initial_probe = None
                        for proposed_level in (self.reading_level + 1,
                                               self.reading_level - 1,
                                               self.reading_level + 2):
                            if not 1 <= proposed_level <= 49:
                                continue
                            sample = read_exp(xp_area, proposed_level)
                            req = requirement(proposed_level)
                            if (sample and req and sample[1] >= 3
                                and abs(sample[0] / req * 100 - sample[1]) < 0.2):
                                initial_probe = (proposed_level, sample)
                                break
                        if initial_probe is not None:
                            candidate, scan = initial_probe
                            if startup_level_candidate == candidate:
                                startup_level_hits += 1
                            else:
                                startup_level_candidate = candidate
                                startup_level_hits = 1
                            if startup_level_hits >= 2:
                                self.capture_messages.put(
                                    ("exp", session_id, candidate, scan[0],
                                     scan[1], scan[2]))
                        else:
                            startup_level_hits = 0
                    # If a new level starts at low percentage, its current EXP
                    # no longer agrees with the preceding level's EXP table.
                    elif (result is None and hunt.current_pct is not None
                            and hunt.current_pct >= 68 and self.reading_level < 50):
                        new_level = read_exp(xp_area, self.reading_level + 1, None)
                        if new_level:
                            retry_level += 1
                            if retry_level >= 2:
                                self.capture_messages.put(
                                    ("exp", session_id, self.reading_level + 1, new_level[0],
                                     new_level[1], new_level[2]))
                        else:
                            retry_level = 0
                    elif result:
                        retry_level = 0
                        startup_level_hits = 0
                        self.capture_messages.put(
                            ("exp", session_id, self.reading_level, result[0], result[1], result[2]))
                    else:
                        self.capture_messages.put(("status", "경험치 글자 인식 중"))
                    if result is not None:
                        self.capture_messages.put(("status", "경험치 고속 자동 추적"))
            except Exception as error:
                self.capture_messages.put(("error", str(error)))
            # Use remaining time only; OCR duration does not add extra delay.
            self.capture_stop.wait(max(0.03, EXP_POLL_INTERVAL -
                                       (time.monotonic() - started)))

    def _meso_worker(self):
        detector = AutoMesoDetector()
        active_session_id = None
        last_full_time = 0.0
        while not self.capture_stop.is_set():
            started = time.monotonic()
            try:
                hunt = self.session
                if hunt is None or hunt.paused or hunt.stopped:
                    self.capture_stop.wait(0.5)
                    continue
                session_id = id(hunt)
                if session_id != active_session_id:
                    active_session_id = session_id
                    detector.reset()
                    last_full_time = 0.0
                info = game_info()
                if info is not None:
                    direct = getattr(self, "window_capture", None)
                    if direct is not None:
                        if not direct.ensure(info.hwnd):
                            self.capture_stop.wait(0.5)
                            continue
                        frame, full_time = direct.full_snapshot()
                        if frame is None or full_time <= last_full_time:
                            self.capture_stop.wait(0.6)
                            continue
                        last_full_time = full_time
                    else:
                        frame = grab_game(info)
                    found = detector.detect(frame)
                    if found is not None:
                        self.capture_messages.put(
                            ("meso", session_id, found.amount, found.confidence))
            except Exception as error:
                # An inventory OCR failure should not change the EXP status.
                self.capture_messages.put(("meso_error", str(error)))
            self.capture_stop.wait(max(0.25, MESO_POLL_INTERVAL -
                                       (time.monotonic() - started)))

    def _accept_meso(self, amount: int, confidence: int = 3):
        hunt = self.session
        if not hunt or hunt.paused or hunt.stopped:
            return
        now = time.monotonic()
        current = hunt.current_meso
        # A clearly abnormal reading such as 60,897 -> 31 was previously
        # accepted after a single OCR frame. Wait for three independent
        # matching scans (and >=3s) before recording very large balance
        # changes. Real substantial purchases are still tracked eventually.
        unusually_large_change = (
            current is not None and current >= 10000
            and (
                (amount < current * .25 and current - amount >= 10000)
                or (amount > current * 4 and amount - current >= 50000)
            )
        )
        needs_confirmations = (
            3 if unusually_large_change else
            2 if confidence < 3 or (current is None and amount < 1000)
            else 1
        )
        if (self.meso_pending != amount
            or now - self.meso_pending_time > 25):
            self.meso_pending = amount
            self.meso_pending_time = now
            self.meso_pending_count = 1
        else:
            self.meso_pending_count = getattr(self, "meso_pending_count", 1) + 1
        if needs_confirmations > 1 and (
            self.meso_pending_count < needs_confirmations
            or (unusually_large_change and now - self.meso_pending_time < 3)
        ):
            return
        self.meso_pending_count = 0
        self.meso_pending = None
        self.meso_pending_time = now
        self.last_meso_visible_at = now
        # Even unchanged amounts are real inventory observations: they extend
        # the measured hunting duration. EXP snapshots must not substitute
        # for MESOS sightings because the inventory may have been closed.
        hunt.add_meso(amount)

    def _ui_tick(self):
        if self.capture_stop.is_set():
            return
        try:
            changed = False
            for _ in range(40):
                try:
                    message = self.capture_messages.get_nowait()
                except queue.Empty:
                    break
                kind = message[0]
                if kind == "exp" and self.session and not self.session.paused:
                    _, sample_session_id, level, exp, pct, raw = message
                    if sample_session_id != id(self.session):
                        continue
                    if level != self.reading_level:
                        self.reading_level = level
                        self.config["level"] = level
                        save_config(self.config)
                    previous = (self.session.current_exp, self.session.current_pct, self.session.level)
                    self.last_ocr_raw = raw
                    accepted = self.session.add_exp(level, exp, pct)
                    if accepted:
                        self.last_valid_exp_at = time.monotonic()
                        self.valid_exp_reads += 1
                    if previous != (self.session.current_exp, self.session.current_pct, self.session.level):
                        changed = True
                elif kind == "meso":
                    if self.session and message[1] == id(self.session):
                        previous = (self.session.current_meso,
                                    self.session.meso_last_seen_elapsed)
                        self._accept_meso(message[2], message[3])
                        if previous != (self.session.current_meso,
                                        self.session.meso_last_seen_elapsed):
                            changed = True
                elif kind == "status":
                    if self.session and message[1] != self.session.ocr_status:
                        self.session.ocr_status = message[1]
                        changed = True
                elif kind == "error":
                    self.ocr_error = message[1]
                    if self.session:
                        self.session.ocr_status = "OCR 오류 (설정 확인)"
                        changed = True
                elif kind == "meso_error":
                    self.ocr_error = "메소 자동 인식: " + message[1]
            now = time.monotonic()
            if changed or now - self.last_render_time >= 1.0:
                self._render_values()
                self.last_render_time = now
            if self.session and time.monotonic() - self.last_saved > 16:
                self.store.save(self.session)
                self.last_saved = time.monotonic()
        except Exception:
            self.ocr_error = traceback.format_exc(limit=4)
        self.after(UI_TICK_MS, self._ui_tick)

    def _render_values(self):
        hunt = self.session
        if not hunt:
            self.status.configure(text="● 대기 · ▶ 시작", text_color=MUTED)
            self.clock_label.configure(text="00:00:00")
            self.exp_minute.value.configure(text="—")
            self.exp_hour.value.configure(text="—")
            self.meso_minute.value.configure(text="—")
            self.meso_hour.value.configure(text="—")
            self.meso_baseline_label.configure(text="기준 메소: —")
            self.meso_detail.configure(text="현재 메소: —")
            self.meso_gain_label.configure(text="누적 +0")
            self.level_label.configure(text=f"Lv. {self.config.get('level', 26)}")
            self.map_label.configure(text=str(self.config.get("map", ""))[:22])
            return
        age = (time.monotonic() - self.last_valid_exp_at
               if self.last_valid_exp_at is not None else None)
        if hunt.paused:
            status = "● 일시정지 · ▶ 눌러 재개"
            status_color = MUTED
        elif age is not None and age < 6:
            status = f"● 경험치 인식 정상 · {age:.1f}초 전 ({self.valid_exp_reads}회)"
            status_color = GREEN
        elif age is not None and age >= 6:
            status = f"● 경험치 인식 끊김 · {age:.0f}초 경과"
            status_color = RED
        else:
            status = "● " + hunt.ocr_status
            status_color = MUTED
        self.status.configure(text=status, text_color=status_color)
        self.clock_label.configure(text=fmt_hms(hunt.elapsed()))
        self.level_label.configure(text=f"Lv. {hunt.level}")
        self.map_label.configure(text=hunt.map_name[:23])
        pct = hunt.current_pct
        self.exp_pct_label.configure(text=f"{pct:.2f}%" if pct is not None else "읽는 중…")
        self.exp_bar.set(min(1, max(0, (pct or 0) / 100)))
        mins = self.period.get()
        exp_rate = hunt.rolling_rate("exp", mins)
        self.exp_minute.value.configure(text=fmt_int(exp_rate / 60 if exp_rate is not None else None))
        self.exp_hour.value.configure(text=fmt_int(exp_rate))
        self.exp_minute.sub.configure(text=f"누적 {fmt_int(hunt.gained_exp,True)} EXP")
        self.exp_hour.sub.configure(text=f"최근 {mins}분 기준")
        current_req = requirement(hunt.level)
        self.exp_detail.configure(
            text=f"현재 {fmt_int(hunt.current_exp)} / {fmt_int(current_req)}"
                 f"  ·  누적 {fmt_int(hunt.gained_exp,True)} EXP")
        self.exp_eta.configure(
            text="레벨업까지 " + eta_text(hunt.until_level(exp_rate)))
        self.exp_chart.update_values(hunt.chart("exp", mins))
        # Never use rolling_rate('meso') here: its EXP samples retain the
        # last visible inventory balance and fabricate an enormous 1-minute
        # pickup when a closed inventory is reopened. Use only confirmed
        # balance observations and the active time between them.
        meso_rate = hunt.meso_session_rate()
        self.meso_minute.value.configure(
            text=fmt_int(meso_rate / 60 if meso_rate is not None else None, signed=True))
        self.meso_hour.value.configure(text=fmt_int(meso_rate, signed=True))
        self.meso_minute.sub.configure(
            text=("확인한 순증감 " + fmt_int(hunt.gained_meso, signed=True)
                  if hunt.initial_meso is not None else "기준 잔액 인식 대기"))
        covered = hunt.meso_observed_seconds
        self.meso_hour.sub.configure(
            text=("관측 사냥 " + fmt_hms(covered) if covered is not None
                  else "인벤토리 잔액 대기"))
        self.meso_gain_label.configure(text="누적 " + fmt_int(hunt.gained_meso, signed=True))
        self.meso_baseline_label.configure(
            text=("기준 메소: " + fmt_int(hunt.initial_meso)
                  if hunt.initial_meso is not None else "기준 메소: 자동 감지 대기"))
        self.meso_detail.configure(
            text=("현재 메소: " + fmt_int(hunt.current_meso)
                  if hunt.current_meso is not None else "현재 메소: —"))
        self.meso_auto_badge.configure(
            text=("● 메소 자동 감지" if hunt.current_meso is None
                  else "● 잔액 급변 재확인 중"
                  if getattr(self, "meso_pending_count", 0) > 0
                  else "● 메소 갱신됨"
                  if self.last_meso_visible_at is not None
                  and time.monotonic() - self.last_meso_visible_at < 12
                  else "● 마지막 잔액"),
            text_color=(GREEN if self.last_meso_visible_at is not None
                        and time.monotonic() - self.last_meso_visible_at < 12
                        else MUTED))
        self.meso_chart.update_values(hunt.meso_average_chart(mins))
        self.notice_text.configure(text=(
            "메소 예상치는 기준·현재 잔액과 관측 사냥시간의 누적 평균"
            if hunt.current_meso is not None else
            "경험치 약 0.5초 갱신  •  메소 잔액은 인벤토리 표시 시 감지"))

    def pick_region(self, kind):
        info = game_info()
        if info is None:
            messagebox.showwarning("게임 감지 실패", "먼저 MapleStory 게임 화면을 켜 주세요.")
            return
        try:
            picture = grab_game(info)
        except Exception as error:
            messagebox.showerror("화면 캡처 실패", str(error))
            return
        if kind == "meso":
            prompt = "메소 잔액이 보이는 숫자 부분만 드래그"
        else:
            prompt = "EXP 숫자와 %를 함께 감싸서 드래그"
        Picker(self, picture, prompt, lambda box: self._save_region(kind, box))

    def _save_region(self, kind, box):
        if kind == "exp":
            self.exp_roi = box
            self.config["exp_roi"] = box
            self.window_capture.set_exp_roi(box)
        else:
            self.meso_roi = box
            self.config["meso_roi"] = box
            self.meso_pending = None
        save_config(self.config)
        self.status.configure(text="● 새 화면 영역 저장 완료")

    def show_settings(self):
        win = ctk.CTkToplevel(self)
        win.title("설정 · 사냥 트래커")
        win.geometry("400x495")
        win.attributes("-topmost", True)
        win.configure(fg_color=BG)
        label(win, "트래커 설정", 20, WHITE, True).pack(anchor="w", padx=18, pady=(17, 15))
        label(win, "캐릭터 레벨 (경험치 OCR 검증·레벨업 계산에 필요)", 12, MUTED).pack(
            anchor="w", padx=18)
        lvl = ctk.CTkEntry(win, width=350)
        lvl.insert(0, str(self.reading_level))
        lvl.pack(fill="x", padx=18, pady=(4, 12))
        label(win, "사냥터 / 기록 이름", 12, MUTED).pack(anchor="w", padx=18)
        map_input = ctk.CTkEntry(win)
        map_input.insert(0, str(self.config.get("map", "")))
        map_input.pack(fill="x", padx=18, pady=(4, 12))
        top_var = ctk.BooleanVar(value=bool(self.config.get("always_top", True)))
        ctk.CTkSwitch(win, text="항상 위에 표시", variable=top_var,
                      progress_color=PURPLE, font=ctk.CTkFont(family="Malgun Gothic", size=12)).pack(
            anchor="w", padx=18, pady=(5, 10))
        button(win, "경험치 숫자 위치 다시 지정", lambda: self.pick_region("exp"),
               width=355, fg="#20273a").pack(padx=16, pady=3)
        label(win, "메소는 인벤토리의 MESOS 표시를 자동으로 찾아 추적합니다.",
              11, GREEN, wraplength=354).pack(anchor="w", padx=18, pady=(7, 5))

        def save():
            try:
                value = int(lvl.get().strip())
                if not 1 <= value <= 100:
                    raise ValueError()
            except ValueError:
                messagebox.showwarning("레벨 입력", "1~100 사이의 레벨을 입력해 주세요.")
                return
            if self.session and self.session.current_exp is not None and self.session.level != value:
                if not messagebox.askyesno("레벨 변경",
                    "진행 중인 사냥 기록의 레벨이 변경됩니다.\n"
                    "경험치 누적 계산을 정확히 하려면 새 세션을 권장합니다. 계속할까요?"):
                    return
            self.reading_level = value
            self.config["level"] = value
            self.config["map"] = map_input.get().strip() or "사냥터 미지정"
            self.config["always_top"] = top_var.get()
            if self.session:
                self.session.level = value
                self.session.map_name = self.config["map"]
            self.attributes("-topmost", top_var.get())
            save_config(self.config)
            win.destroy()
            self._render_values()

        button(win, "설정 저장", save, width=355, height=36, fg=PURPLE).pack(
            padx=16, pady=(12, 7))
        label(win, f"기록 위치: {BASE}", 10, MUTED, wraplength=362).pack(
            anchor="w", padx=18)
        if self.ocr_error:
            label(win, "최근 OCR 오류: " + self.ocr_error[:145], 10, RED,
                  wraplength=360).pack(anchor="w", padx=18, pady=4)

    def show_history(self):
        win = ctk.CTkToplevel(self)
        win.title("사냥 기록")
        win.geometry("620x595")
        win.attributes("-topmost", True)
        win.configure(fg_color=BG)
        header = ctk.CTkFrame(win, fg_color="transparent")
        header.pack(fill="x", padx=17, pady=14)
        label(header, "사냥 기록", 20, WHITE, True).pack(side="left")
        button(header, "CSV 내보내기", lambda: self.export_csv(win),
               width=113, fg=PURPLE).pack(side="right")
        scroll = ctk.CTkScrollableFrame(win, fg_color=BG)
        scroll.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        rows = self.store.recent()
        if not rows:
            label(scroll, "아직 사냥 기록이 없습니다.", 14, MUTED).pack(pady=30)
        for row in rows:
            area = ctk.CTkFrame(scroll, fg_color=CARD, corner_radius=12)
            area.pack(fill="x", pady=4)
            try:
                date = datetime.fromisoformat(row["started"]).strftime("%m/%d %H:%M")
            except (TypeError, ValueError):
                date = row["started"]
            label(area, f"{date}   ·   {row['map']}", 13, WHITE, True).pack(
                anchor="w", padx=12, pady=(8, 2))
            label(area, f"Lv.{row['level_start']} → {row['level_end'] or '?'}"
                  f"    {fmt_hms(row['duration'] or 0)}"
                  f"    경험치 {fmt_int(row['gained_exp'],True)}"
                  f"    메소 {fmt_int(row['gained_meso'],True)}",
                  11, GREEN).pack(anchor="w", padx=12, pady=(0, 9))

    def export_csv(self, win=None):
        filename = f"maple_hunting_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
        path = filedialog.asksaveasfilename(
            parent=win or self, title="기록 CSV 저장",
            initialfile=filename, defaultextension=".csv",
            filetypes=[("CSV 파일", "*.csv")])
        if not path:
            return
        try:
            count = self.store.export(Path(path))
            messagebox.showinfo("내보내기 완료", f"{count}개 세션을 CSV로 저장했어.\n{path}")
        except Exception as error:
            messagebox.showerror("저장 실패", str(error))

    def quit_app(self):
        self.capture_stop.set()
        self.window_capture.stop()
        if self.session and not self.session.stopped:
            self.store.finish(self.session)
        self.store.close()
        self.config["geometry"] = [self.winfo_x(), self.winfo_y()]
        save_config(self.config)
        self.destroy()

    def report_callback_exception(self, exc, val, tb):
        BASE.mkdir(parents=True, exist_ok=True)
        with (BASE / "errors.log").open("a", encoding="utf-8") as file:
            file.write(f"\n[{datetime.now().isoformat(timespec='seconds')}]\n")
            file.write("".join(traceback.format_exception(exc, val, tb)))
        self.ocr_error = str(val)
        try:
            messagebox.showerror("사냥 트래커 오류",
                                 "프로그램 오류가 발생했습니다.\n"
                                 "오류 기록: %LOCALAPPDATA%\\MapleHuntTracker\\errors.log\n\n"
                                 + str(val))
        except Exception:
            pass


def self_test():
    from PIL import Image
    from screen import parse_exp_text, parse_meso_text
    from engine import accumulated
    assert parse_exp_text("EXP 19,497[35.51%]") == (19497, 35.51)
    assert parse_exp_text("19,497[35.51 %]") == (19497, 35.51)
    assert parse_meso_text("메소 1,245,987") == 1245987
    assert accumulated(26, 19497) == 292768
    assert requirement(26) == 54900
    print("MAPLE_HUNT_TRACKER_SELF_TEST_OK", VERSION)


def packaged_ocr_test() -> int:
    """Test WGC-based hidden-game OCR in the exact packaged executable."""
    info = game_info()
    candidate = None
    detected_level = None
    configured_level = int(load_config().get("level", 26))
    direct = GameWindowCapture(load_config().get("exp_roi", DEFAULT_EXP_REGION))
    try:
        if info and direct.ensure(info.hwnd):
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline and candidate is None:
                region, timestamp = direct.exp_snapshot()
                if region is not None:
                    for candidate_level in (configured_level, configured_level + 1,
                                            configured_level - 1, configured_level + 2):
                        if not 1 <= candidate_level <= 49:
                            continue
                        candidate = read_exp(region, candidate_level)
                        if candidate:
                            detected_level = candidate_level
                            break
                if candidate is None:
                    time.sleep(0.35)
    finally:
        capture_error = direct.error
        direct.stop()
    report = {
        "game_found": info is not None, "ocr_installed": ocr_available(),
        "capture_method": "Windows.Graphics.Capture direct MapleStory window",
        "capture_error": capture_error,
        "configured_level": configured_level,
        "read_level": detected_level,
        "exp_sample": candidate[0] if candidate else None,
        "percentage": candidate[1] if candidate else None,
        "ocr_text": candidate[2] if candidate else None
    }
    BASE.mkdir(parents=True, exist_ok=True)
    (BASE / "packaged_ocr_qa.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if candidate else 2


def packaged_meso_test() -> int:
    """Exercise the frozen executable's real Windows OCR with movable GUI."""
    from qa_fixture import create_inventory_fixture
    canvas = create_inventory_fixture(x=975, y=100, amount=50792)
    observed = AutoMesoDetector().detect(canvas)
    report = {"version": VERSION,
              "auto_meso_ocr_pass": observed is not None and observed.amount == 50792,
              "detected": observed.amount if observed is not None else None}
    BASE.mkdir(parents=True, exist_ok=True)
    (BASE / "packaged_auto_meso_qa.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["auto_meso_ocr_pass"] else 3


def packaged_session_test() -> int:
    """Exercise the actual Tk/UI message pump without touching user records."""
    import tempfile
    global BASE, CONFIG_PATH

    real_base = BASE
    real_config = CONFIG_PATH
    report = {"version": VERSION, "status": "UNTESTED"}
    try:
        with tempfile.TemporaryDirectory(prefix="maple-live-ui-qa-") as temp:
            BASE = Path(temp)
            CONFIG_PATH = BASE / "settings.json"
            settings = dict(load_config())
            # Real game Lv 27 when QA was authored; prefer user setting and
            # permit automatic startup correction in the actual app.
            settings.update({"level": 27, "map": "독립 기능 테스트",
                             "exp_roi": DEFAULT_EXP_REGION})
            save_config(settings)
            tracker = TrackerApp()
            tracker.withdraw()
            tracker.new_session()

            def done():
                hunt = tracker.session
                # Verify the actual UI renders the initial balance persistently,
                # even when the live inventory isn't currently visible.
                if hunt is not None:
                    if hunt.initial_meso is None:
                        tracker._accept_meso(60464, 3)
                    baseline_before = hunt.initial_meso
                    # This is a temporary, isolated session: simulate one
                    # full minute of actual hunting between real inventory
                    # observations, with no user database changes.
                    hunt.start_clock -= 60
                    tracker._accept_meso(hunt.current_meso + 448, 3)
                    tracker._render_values()
                else:
                    baseline_before = None
                report.update({
                    "valid_exp_reads": tracker.valid_exp_reads,
                    "exp_value": hunt.current_exp if hunt else None,
                    "exp_percentage": hunt.current_pct if hunt else None,
                    "level": hunt.level if hunt else None,
                    "ocr_status": hunt.ocr_status if hunt else None,
                    "ocr_error": tracker.ocr_error,
                    "session_elapsed": round(hunt.elapsed(), 1) if hunt else None,
                    "meso_baseline": hunt.initial_meso if hunt else None,
                    "meso_current": hunt.current_meso if hunt else None,
                    "meso_net_gain": hunt.gained_meso if hunt else None,
                    "meso_observed_seconds": hunt.meso_observed_seconds if hunt else None,
                    "meso_rate_per_hour": hunt.meso_session_rate() if hunt else None,
                    "meso_minute_ui": tracker.meso_minute.value.cget("text"),
                    "meso_hour_ui": tracker.meso_hour.value.cget("text"),
                    "meso_baseline_ui": tracker.meso_baseline_label.cget("text"),
                    "meso_current_ui": tracker.meso_detail.cget("text"),
                })
                report["status"] = (
                    "PASS" if tracker.valid_exp_reads >= 3
                    and hunt is not None and hunt.current_exp is not None
                    and baseline_before == hunt.initial_meso
                    and f"{hunt.initial_meso:,}" in tracker.meso_baseline_label.cget("text")
                    and f"{hunt.current_meso:,}" in tracker.meso_detail.cget("text")
                    and hunt.gained_meso >= 448
                    and hunt.meso_observed_seconds >= 60
                    and tracker.meso_minute.value.cget("text") ==
                        fmt_int(hunt.meso_session_rate() / 60, signed=True)
                    and tracker.meso_hour.value.cget("text") ==
                        fmt_int(hunt.meso_session_rate(), signed=True)
                    else "FAIL"
                )
                tracker.quit_app()

            tracker.after(6200, done)
            tracker.mainloop()
    finally:
        BASE = real_base
        CONFIG_PATH = real_config
    BASE.mkdir(parents=True, exist_ok=True)
    (BASE / "packaged_live_session_qa.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["status"] == "PASS" else 4


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
    elif "--ocr-test" in sys.argv:
        raise SystemExit(packaged_ocr_test())
    elif "--meso-test" in sys.argv:
        raise SystemExit(packaged_meso_test())
    elif "--session-test" in sys.argv:
        raise SystemExit(packaged_session_test())
    else:
        BASE.mkdir(parents=True, exist_ok=True)
        TrackerApp().mainloop()
