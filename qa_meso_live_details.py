from __future__ import annotations
import time
from pathlib import Path
from PIL import Image
from winocr import recognize_pil_sync
from screen import game_info, DEFAULT_EXP_REGION
from window_capture import GameWindowCapture

c=GameWindowCapture(DEFAULT_EXP_REGION)
assert c.ensure(game_info().hwnd)
time.sleep(1.2)
frame, stamp = c.full_snapshot()
assert frame
print("LIVE_SIZE",frame.size)
root=Path("debug")
regions=[
    ("inventory_zoom",frame.crop((1000,0,1850,900))),
    ("inventory_footer",frame.crop((1120,675,1760,820))),
    ("upper_half",frame.crop((0,0,frame.width,int(frame.height*.52))).resize((2400,702))),
]
for name,img in regions:
    img.save(root/(name+"_live.png"))
    for lang in ("ko","en"):
        try:
            recognized=recognize_pil_sync(img,lang=lang)
            print(name,lang,[(ascii(x.get('text','')),x['words'][0].get('bounding_rect')) for x in recognized.get('lines',[]) if x.get('words')][:50],flush=True)
        except Exception as e:
            print(name,lang,"ERROR",str(e))
c.stop()
