"""Diagnostic test of PrintWindow against a background MapleStory window.

This is a screen-only/window-pixel test. Never reads game process memory.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path
from PIL import Image
from screen import game_info, read_exp, DEFAULT_EXP_REGION

user32 = ctypes.windll.user32
gdi = ctypes.windll.gdi32


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def print_window(hwnd: int, w: int, h: int, flag: int):
    hdc = user32.GetDC(hwnd)
    mem = gdi.CreateCompatibleDC(hdc)
    bitmap = gdi.CreateCompatibleBitmap(hdc, w, h)
    old = gdi.SelectObject(mem, bitmap)
    try:
        printed = user32.PrintWindow(hwnd, mem, flag)
        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = w
        info.bmiHeader.biHeight = -h
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0
        buffer = ctypes.create_string_buffer(w * h * 4)
        line_count = gdi.GetDIBits(mem, bitmap, 0, h, buffer, ctypes.byref(info), 0)
        frame = Image.frombuffer("RGB", (w, h), buffer, "raw", "BGRX", 0, 1).copy()
        return printed, line_count, frame
    finally:
        gdi.SelectObject(mem, old)
        gdi.DeleteObject(bitmap)
        gdi.DeleteDC(mem)
        user32.ReleaseDC(hwnd, hdc)


def main():
    info = game_info()
    print("MAPLE_HWND", info.hwnd, "RECT", info.bbox, flush=True)
    out = Path(__file__).resolve().parent / "debug"
    out.mkdir(exist_ok=True)
    for flag in (2, 0):
        printed, lines, frame = print_window(info.hwnd, info.width, info.height, flag)
        roi = frame.crop((
            int(info.width * DEFAULT_EXP_REGION[0]), int(info.height * DEFAULT_EXP_REGION[1]),
            int(info.width * DEFAULT_EXP_REGION[2]), int(info.height * DEFAULT_EXP_REGION[3]),
        ))
        roi.save(out / f"background_printwindow_{flag}.png")
        print("FLAG", flag, "PRINTWINDOW", printed, "DIBITS", lines,
              "CENTER", frame.getpixel((frame.width // 2, frame.height // 2)),
              "EXP", read_exp(roi, 27), flush=True)


if __name__ == "__main__":
    main()
