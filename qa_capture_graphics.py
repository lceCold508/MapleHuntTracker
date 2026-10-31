"""Test Windows.Graphics.Capture against occluded MapleStory window.

Read-only screen capture; does not interact with game process or keyboard.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from PIL import Image
from windows_capture import WindowsCapture
from screen import game_info, read_exp, DEFAULT_EXP_REGION

def main():
    info = game_info()
    print("MAPLE_HWND", info.hwnd, "CLIENT", info.width, info.height, flush=True)
    lock = threading.Event()
    result = {}
    capture = WindowsCapture(window_hwnd=info.hwnd, cursor_capture=False, draw_border=False)
    @capture.event
    def on_frame_arrived(frame, control):
        try:
            print("FRAME_OBJECT", [a for a in dir(frame) if not a.startswith("_")], flush=True)
            print("FRAME_DIM", frame.width, frame.height, flush=True)
            rect = (int(frame.width*DEFAULT_EXP_REGION[0]),
                    int(frame.height*DEFAULT_EXP_REGION[1]),
                    int(frame.width*DEFAULT_EXP_REGION[2]),
                    int(frame.height*DEFAULT_EXP_REGION[3]))
            small = frame.crop(*rect)
            buf = small.frame_buffer
            print("CROPPED_BUFFER", type(buf), "DIM", small.width, small.height,
                  "BUFFER_LEN", len(buf), "FIRST", repr(buf[:10]),flush=True)
            path = Path(__file__).parent / "debug" / "maple_window_wgc.png"
            frame.save_as_image(str(path))
            img = Image.open(path).convert("RGB")
            w,h=img.size
            region = img.crop(tuple(map(int, (
                w*DEFAULT_EXP_REGION[0],h*DEFAULT_EXP_REGION[1],
                w*DEFAULT_EXP_REGION[2],h*DEFAULT_EXP_REGION[3]
            ))))
            region.save(Path(__file__).parent / "debug" / "maple_window_wgc_exp.png")
            result["exp"] = read_exp(region, 27)
            print("CAPTURE_RESULT", result, flush=True)
        except Exception as e:
            result["exception"] = repr(e)
            print("CAPTURE_EXCEPTION", repr(e), flush=True)
        finally:
            lock.set()
            control.stop()
    @capture.event
    def on_closed():
        print("CAPTURE_WINDOW_CLOSED", flush=True)
        lock.set()
    try:
        ctrl = capture.start_free_threaded()
        if not lock.wait(6):
            print("CAPTURE_TIMEOUT",flush=True)
        try: ctrl.stop()
        except Exception as e: print("CONTROL_STOP_ISSUE",str(e),flush=True)
    except Exception as e:
        print("CAPTURE_START_FAILED",type(e).__name__,str(e),flush=True)
    return 0 if result.get("exp") else 2

if __name__=="__main__":
    raise SystemExit(main())
