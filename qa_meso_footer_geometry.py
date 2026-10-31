"""Read-only local QA to validate full ITEM INVENTORY/footer geometry."""
from PIL import Image, ImageStat
from meso_detector import _recognize, _inventory_title_rect, _amount_from_number_only_crop

img = Image.open("debug/inventory_zoom_live.png").convert("RGB")
title = _inventory_title_rect(_recognize(img))
print("TITLE", title)
x, y, x2, y2 = title
print("TITLE_WIDTH_SCALE", (x2-x)/204, "FONT_HEIGHT_SCALE", (y2-y)/16)
scale = (x2-x)/204.0
for box_name, offsets in [
    ("number", (305, 708, 465, 780)),
    ("label", (446, 709, 561, 783)),
    ("footer_full", (30, 705, 565, 789)),
    ("label_tight", (445, 726, 553, 771)),
    ("number_tight", (320, 727, 462, 771)),
    ("wrong_scale_number", (305, 708, 465, 780))
]:
    factor = scale if box_name != "wrong_scale_number" else 0.65
    xa, ya, xb, yb = [int(v) for v in (x+offsets[0]*factor,y+offsets[1]*factor,
                                       x+offsets[2]*factor,y+offsets[3]*factor)]
    crop = img.crop((xa,ya,xb,yb))
    print(box_name, (xa,ya,xb,yb), "AVG_RGB",
          tuple(round(v) for v in ImageStat.Stat(crop).mean),
          "TEXT", [ascii(l.get("text")) for l in _recognize(crop).get("lines",[])],
          "AMOUNT",_amount_from_number_only_crop(crop))
    crop.save("debug/qa_"+box_name+".png")
