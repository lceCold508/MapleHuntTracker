"""Capture MapleStory's window directly, even when another app obscures it.

Uses the Windows Graphics Capture API; this is a pixels-only capture. It
does not read process memory, keyboard input, network traffic or game files.
"""
from __future__ import annotations

import threading
import time
from PIL import Image


class GameWindowCapture:
    def __init__(self, exp_roi):
        self._lock = threading.Lock()
        self._manager_lock = threading.Lock()
        self._exp_roi = tuple(exp_roi)
        # The game HUD places buffs in the top-right. Capture a separate
        # small crop for frequent icon detection without affecting EXP OCR.
        self._buff_roi = (0.60, 0.0, 1.0, 0.12)
        self._hwnd = None
        self._control = None
        self._capture = None
        self._exp_image = None
        self._exp_time = 0.0
        self._buff_image = None
        self._buff_time = 0.0
        self._full_image = None
        self._full_time = 0.0
        self._last_full_copy = 0.0
        self._last_exp_copy = 0.0
        self._last_buff_copy = 0.0
        self.error = ""
        self.closed = False

    @staticmethod
    def _to_pil(frame):
        # The Windows Capture library presents pixels as BGRA. Keep the
        # image independent of the GPU frame after its callback returns.
        buf = frame.frame_buffer
        return Image.frombytes("RGB", (frame.width, frame.height),
                               buf.tobytes(), "raw", "BGRX", 0, 1)

    def set_exp_roi(self, roi):
        with self._lock:
            self._exp_roi = tuple(roi)
            self._exp_image = None
            self._exp_time = 0.0

    def ensure(self, hwnd):
        with self._manager_lock:
            if self._hwnd == hwnd and self._control is not None and not self.closed:
                return True
            self._stop_locked()
            try:
                from windows_capture import WindowsCapture
                capture = WindowsCapture(window_hwnd=hwnd, cursor_capture=False,
                                         draw_border=False,
                                         minimum_update_interval=100)

                @capture.event
                def on_frame_arrived(frame, control):
                    now = time.monotonic()
                    with self._lock:
                        roi = self._exp_roi
                        exp_due = now - self._last_exp_copy >= 0.28
                        buff_due = now - self._last_buff_copy >= 0.18
                        full_due = now - self._last_full_copy >= 3.1
                    try:
                        exp = None
                        buff = None
                        full = None
                        if exp_due:
                            x1,y1,x2,y2 = roi
                            box = (max(0, int(x1 * frame.width)),
                                   max(0, int(y1 * frame.height)),
                                   min(frame.width, int(x2 * frame.width)),
                                   min(frame.height, int(y2 * frame.height)))
                            if box[2]-box[0] >= 15 and box[3]-box[1] >= 10:
                                exp = self._to_pil(frame.crop(*box))
                        if buff_due:
                            bx1,by1,bx2,by2 = self._buff_roi
                            buff_box = (int(bx1 * frame.width),
                                        int(by1 * frame.height),
                                        int(bx2 * frame.width),
                                        int(by2 * frame.height))
                            buff = self._to_pil(frame.crop(*buff_box))
                        if full_due:
                            full = self._to_pil(frame)
                        with self._lock:
                            if exp is not None:
                                self._exp_image = exp
                                self._exp_time = now
                                self._last_exp_copy = now
                            if buff is not None:
                                self._buff_image = buff
                                self._buff_time = now
                                self._last_buff_copy = now
                            if full is not None:
                                self._full_image = full
                                self._full_time = now
                                self._last_full_copy = now
                    except Exception as error:
                        with self._lock:
                            self.error = f"캡처 프레임 처리 실패: {error}"

                @capture.event
                def on_closed():
                    with self._lock:
                        self.closed = True
                        self.error = "게임 창 캡처 종료됨"

                self._control = capture.start_free_threaded()
                self._capture = capture
                self._hwnd = hwnd
                self.closed = False
                self.error = ""
                return True
            except Exception as error:
                self.closed = True
                self.error = f"게임 창 캡처 시작 실패: {error}"
                return False

    def exp_snapshot(self):
        with self._lock:
            return self._exp_image, self._exp_time

    def buff_snapshot(self):
        with self._lock:
            return self._buff_image, self._buff_time

    def full_snapshot(self):
        with self._lock:
            return self._full_image, self._full_time

    def _stop_locked(self):
        control = self._control
        self._control = None
        self._capture = None
        self._hwnd = None
        with self._lock:
            self._exp_image = None
            self._exp_time = 0
            self._buff_image = None
            self._buff_time = 0
            self._full_image = None
            self._full_time = 0
            self._last_exp_copy = 0
            self._last_full_copy = 0
            self._last_buff_copy = 0
        if control is not None:
            try:
                control.stop()
            except Exception:
                pass

    def stop(self):
        with self._manager_lock:
            self._stop_locked()
