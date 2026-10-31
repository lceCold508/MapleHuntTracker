"""Zoomable, pixel-accurate EXP HUD selection for high-resolution game windows.

Only the visible canvas is rendered; zooming a 4K screenshot never allocates
a 4K x 4 zoomed PhotoImage. Selection stays in original image coordinates.
"""
from __future__ import annotations

import math
import tkinter as tk
from collections.abc import Callable

from PIL import Image, ImageTk


class PickerView:
    """Source-image viewport independent of Tk/DPI and canvas resolution."""

    def __init__(self, image_size: tuple[int, int], viewport_size: tuple[int, int]):
        self.width, self.height = image_size
        self.viewport_width, self.viewport_height = viewport_size
        if min(self.width, self.height, self.viewport_width, self.viewport_height) <= 0:
            raise ValueError("Image and viewport dimensions must be positive")
        self.zoom = 1.0
        self.offset_x = 0.0
        self.offset_y = 0.0
        self.fit()

    def _clamp(self):
        for extent, viewport, attr in (
            (self.width, self.viewport_width, "offset_x"),
            (self.height, self.viewport_height, "offset_y"),
        ):
            visible = viewport / self.zoom
            if visible >= extent:
                setattr(self, attr, (extent - visible) / 2)
            else:
                setattr(self, attr, min(extent - visible, max(0.0, getattr(self, attr))))

    def fit(self):
        self.zoom = min(self.viewport_width / self.width,
                        self.viewport_height / self.height)
        self.offset_x = self.offset_y = 0.0
        self._clamp()

    def center(self, source_x: float, source_y: float, zoom: float | None = None):
        if zoom is not None:
            self.zoom = min(5.0, max(0.05, float(zoom)))
        self.offset_x = source_x - self.viewport_width / (2 * self.zoom)
        self.offset_y = source_y - self.viewport_height / (2 * self.zoom)
        self._clamp()

    def center_on_roi(self, roi: list[float] | tuple[float, ...]):
        x1, y1, x2, y2 = roi
        roi_width = max(1.0, (x2 - x1) * self.width)
        roi_height = max(1.0, (y2 - y1) * self.height)
        # Open with the EXP text around 2x its original size where possible.
        # A generous margin leaves room to reselect the entire EXP + percent.
        target = min(2.0, self.viewport_width / (roi_width * 1.2),
                     self.viewport_height / (roi_height * 2.5))
        target = max(self.zoom, target)
        self.center((x1 + x2) * self.width / 2,
                    (y1 + y2) * self.height / 2, zoom=target)

    def zoom_at(self, zoom: float, canvas_x: float, canvas_y: float):
        previous = self.zoom
        new = min(5.0, max(0.05, zoom))
        if math.isclose(previous, new, rel_tol=1e-9):
            return
        image_x, image_y = self.to_source(canvas_x, canvas_y, clamp=False)
        self.zoom = new
        self.offset_x = image_x - canvas_x / self.zoom
        self.offset_y = image_y - canvas_y / self.zoom
        self._clamp()

    def pan(self, delta_canvas_x: float, delta_canvas_y: float):
        self.offset_x -= delta_canvas_x / self.zoom
        self.offset_y -= delta_canvas_y / self.zoom
        self._clamp()

    def resize(self, viewport_width: int, viewport_height: int):
        center_x, center_y = self.to_source(self.viewport_width / 2,
                                           self.viewport_height / 2, clamp=False)
        self.viewport_width = max(1, viewport_width)
        self.viewport_height = max(1, viewport_height)
        self.center(center_x, center_y)

    def to_source(self, canvas_x: float, canvas_y: float,
                  clamp: bool = True) -> tuple[float, float]:
        x = self.offset_x + canvas_x / self.zoom
        y = self.offset_y + canvas_y / self.zoom
        if clamp:
            x = min(self.width, max(0.0, x))
            y = min(self.height, max(0.0, y))
        return x, y

    def to_canvas(self, source_x: float, source_y: float) -> tuple[float, float]:
        return (source_x - self.offset_x) * self.zoom, (source_y - self.offset_y) * self.zoom

    def normalized_selection(self, source_a: tuple[float, float],
                             source_b: tuple[float, float]) -> list[float] | None:
        x1, x2 = sorted((min(self.width, max(0, source_a[0])),
                         min(self.width, max(0, source_b[0]))))
        y1, y2 = sorted((min(self.height, max(0, source_a[1])),
                         min(self.height, max(0, source_b[1]))))
        # OCR needs a real screenshot area, not a 2x2 pixel drag.
        if x2 - x1 < 16 or y2 - y1 < 8:
            return None
        return [x1 / self.width, y1 / self.height,
                x2 / self.width, y2 / self.height]


class ExpRegionPicker(tk.Toplevel):
    """EXP region picker with mouse-wheel zoom and right-drag panning."""

    def __init__(self, parent: tk.Misc, snapshot: Image.Image,
                 current_roi: list[float], on_selected: Callable[[list[float]], None]):
        super().__init__(parent)
        self.title("경험치 OCR 영역 선택 · 확대")
        self.configure(bg="#1a2633")
        self.attributes("-topmost", True)
        self.resizable(True, True)
        self.minsize(580, 360)
        self.geometry(f"{min(1320, max(600, self.winfo_screenwidth() - 100))}x"
                      f"{min(920, max(470, self.winfo_screenheight() - 120))}")
        self.snapshot = snapshot.convert("RGB")
        self.current_roi = current_roi
        self.on_selected = on_selected
        self.view: PickerView | None = None
        self.selection_start: tuple[float, float] | None = None
        self.selection_end: tuple[float, float] | None = None
        self.pan_start: tuple[int, int] | None = None
        self.render_after: str | None = None
        self.image_item: int | None = None
        self.image_ref: ImageTk.PhotoImage | None = None

        toolbar = tk.Frame(self, bg="#1a2633", pady=8)
        toolbar.pack(side="top", fill="x", padx=12)
        self._button(toolbar, "－ 축소", lambda: self.change_zoom(1 / 1.5)).pack(side="left", padx=3)
        self._button(toolbar, "＋ 확대", lambda: self.change_zoom(1.5)).pack(side="left", padx=3)
        self._button(toolbar, "EXP 위치로", self.focus_exp).pack(side="left", padx=5)
        self._button(toolbar, "전체 보기", self.fit_image).pack(side="left", padx=3)
        self.zoom_label = tk.Label(toolbar, text="100%", bg="#1a2633", fg="#8efaac",
                                   font=("Malgun Gothic", 11, "bold"), width=9)
        self.zoom_label.pack(side="left", padx=(10, 0))
        self._button(toolbar, "취소", self.destroy).pack(side="right", padx=3)

        guide = tk.Label(self,
            text="휠: 확대/축소  |  우클릭 드래그: 이동  |  좌클릭 드래그: EXP 숫자+% 선택  |  노랑: 이전 선택",
            bg="#1a2633", fg="#f4f7fa", font=("Malgun Gothic", 10))
        guide.pack(side="top", pady=(0, 8))

        self.canvas = tk.Canvas(self, bg="#111923", highlightthickness=0, bd=0,
                                cursor="crosshair")
        self.canvas.pack(side="top", fill="both", expand=True, padx=12, pady=(0, 12))
        self.canvas.bind("<Configure>", self.on_resize)
        self.canvas.bind("<MouseWheel>", self.on_wheel)
        self.canvas.bind("<ButtonPress-1>", self.select_start)
        self.canvas.bind("<B1-Motion>", self.select_motion)
        self.canvas.bind("<ButtonRelease-1>", self.select_finish)
        self.canvas.bind("<ButtonPress-3>", self.pan_begin)
        self.canvas.bind("<B3-Motion>", self.pan_motion)
        self.canvas.bind("<ButtonRelease-3>", self.pan_end)
        self.bind("<Escape>", lambda _event: self.destroy())
        self.bind("<plus>", lambda _event: self.change_zoom(1.5))
        self.bind("<minus>", lambda _event: self.change_zoom(1 / 1.5))
        self.bind("<Left>", lambda _event: self.pan_key(90, 0))
        self.bind("<Right>", lambda _event: self.pan_key(-90, 0))
        self.bind("<Up>", lambda _event: self.pan_key(0, 90))
        self.bind("<Down>", lambda _event: self.pan_key(0, -90))

    @staticmethod
    def _button(parent: tk.Misc, text: str, command: Callable):
        return tk.Button(parent, text=text, command=command,
                         bg="#304153", fg="#f4f7fa",
                         activebackground="#435a6e", activeforeground="white",
                         relief="flat", font=("Malgun Gothic", 10),
                         padx=10, pady=6)

    def on_resize(self, event):
        width, height = event.width, event.height
        if width < 100 or height < 100:
            return
        if self.view is None:
            self.view = PickerView(self.snapshot.size, (width, height))
            self.view.center_on_roi(self.current_roi)
        elif self.view.viewport_width != width or self.view.viewport_height != height:
            self.view.resize(width, height)
        # Tk sends multiple Configure events during window resize.
        if self.render_after:
            self.after_cancel(self.render_after)
        self.render_after = self.after(45, self.render)

    def render(self):
        self.render_after = None
        if self.view is None or not self.winfo_exists():
            return
        m = self.view
        # Resample the portion visible in the canvas instead of resizing the
        # entire 3840x2160 screenshot to a potentially enormous bitmap.
        frame = self.snapshot.transform(
            (m.viewport_width, m.viewport_height), Image.Transform.AFFINE,
            (1.0 / m.zoom, 0.0, m.offset_x, 0.0, 1.0 / m.zoom, m.offset_y),
            resample=Image.Resampling.NEAREST if m.zoom >= 1.0
            else Image.Resampling.BILINEAR,
            fillcolor=(17, 25, 35),
        )
        self.image_ref = ImageTk.PhotoImage(frame)
        if self.image_item is None:
            self.image_item = self.canvas.create_image(
                0, 0, anchor="nw", image=self.image_ref, tags="game-image")
        else:
            self.canvas.itemconfigure(self.image_item, image=self.image_ref)
        self.canvas.tag_lower(self.image_item)
        self.zoom_label.configure(text=f"{m.zoom * 100:.0f}%")
        self.canvas.delete("roi-outline")
        self.canvas.delete("select-outline")
        x1, y1, x2, y2 = self.current_roi
        a = m.to_canvas(x1 * m.width, y1 * m.height)
        b = m.to_canvas(x2 * m.width, y2 * m.height)
        self.canvas.create_rectangle(*a, *b, outline="#ffbf70", width=2,
                                     tags="roi-outline")
        if self.selection_start is not None and self.selection_end is not None:
            self.draw_selection()

    def draw_selection(self):
        self.canvas.delete("select-outline")
        if not self.view or self.selection_start is None or self.selection_end is None:
            return
        a = self.view.to_canvas(*self.selection_start)
        b = self.view.to_canvas(*self.selection_end)
        self.canvas.create_rectangle(*a, *b, outline="#8efaac", width=3,
                                     tags="select-outline")

    def focus_exp(self):
        if self.view is not None:
            self.view.center_on_roi(self.current_roi)
            self.render()

    def fit_image(self):
        if self.view is not None:
            self.view.fit()
            self.render()

    def change_zoom(self, ratio: float, at: tuple[int, int] | None = None):
        if self.view is None:
            return
        x, y = at if at else (self.view.viewport_width / 2,
                             self.view.viewport_height / 2)
        self.view.zoom_at(self.view.zoom * ratio, x, y)
        self.render()

    def on_wheel(self, event):
        if event.delta:
            self.change_zoom(1.3 if event.delta > 0 else 1 / 1.3,
                             (event.x, event.y))
        return "break"

    def pan_begin(self, event):
        self.pan_start = event.x, event.y
        self.canvas.configure(cursor="fleur")

    def pan_motion(self, event):
        if self.view is not None and self.pan_start is not None:
            x, y = self.pan_start
            self.view.pan(event.x - x, event.y - y)
            self.pan_start = event.x, event.y
            self.render()

    def pan_end(self, _event):
        self.pan_start = None
        self.canvas.configure(cursor="crosshair")

    def pan_key(self, dx: int, dy: int):
        if self.view is not None:
            self.view.pan(dx, dy)
            self.render()

    def select_start(self, event):
        if self.view is None:
            return
        self.selection_start = self.view.to_source(event.x, event.y)
        self.selection_end = self.selection_start
        self.draw_selection()

    def select_motion(self, event):
        if self.view is not None and self.selection_start is not None:
            self.selection_end = self.view.to_source(event.x, event.y)
            self.draw_selection()

    def select_finish(self, event):
        if self.view is None or self.selection_start is None:
            return
        self.selection_end = self.view.to_source(event.x, event.y)
        roi = self.view.normalized_selection(self.selection_start, self.selection_end)
        if roi is None:
            self.selection_start = self.selection_end = None
            self.canvas.delete("select-outline")
            return
        self.destroy()
        self.on_selected(roi)

    def destroy(self):
        if self.render_after is not None:
            self.after_cancel(self.render_after)
            self.render_after = None
        super().destroy()
