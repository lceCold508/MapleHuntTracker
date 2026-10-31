import ctypes
from ctypes import wintypes
from pathlib import Path
from PIL import ImageGrab, ImageEnhance, ImageOps, ImageFilter
from winocr import recognize_pil_sync

ROOT=Path(__file__).resolve().parent
user32=ctypes.windll.user32
user32.SetProcessDPIAware()
hwnd=user32.FindWindowW(None,"MapleStory")
if not hwnd:
    raise SystemExit("GAME_NOT_FOUND")
rect=wintypes.RECT()
user32.GetClientRect(hwnd,ctypes.byref(rect))
pt=wintypes.POINT(0,0)
user32.ClientToScreen(hwnd,ctypes.byref(pt))
box=(pt.x,pt.y,pt.x+rect.right,pt.y+rect.bottom)
im=ImageGrab.grab(bbox=box,all_screens=True)
print("SCREEN",box,im.size)
dbg=ROOT/"debug";dbg.mkdir(exist_ok=True)
im.save(dbg/"game.png")
w,h=im.size
for name,region in [
    ("exp",(int(w*.52),int(h*.943),int(w*.64),h)),
    ("exp_wide",(int(w*.49),int(h*.935),int(w*.66),h)),
    ("bottom",(0,int(h*.91),w,h))
]:
    crop=im.crop(region)
    crop.save(dbg/f"{name}_orig.png")
    for variant,src in [
        ("normal",crop.resize((crop.width*4,crop.height*4))),
        ("gray",ImageEnhance.Contrast(ImageOps.grayscale(crop).resize((crop.width*5,crop.height*5))).enhance(2.0)),
        ("bw",ImageOps.grayscale(crop).resize((crop.width*5,crop.height*5)).point(lambda x:255 if x>135 else 0)),
    ]:
        src.save(dbg/f"{name}_{variant}.png")
        try:
            result=recognize_pil_sync(src,lang="ko")
            print(name,variant,str(result)[:1000])
        except Exception as e:
            print(name,variant,type(e).__name__,str(e)[:160])
