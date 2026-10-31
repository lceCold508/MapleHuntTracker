"""Synthetic ITEM INVENTORY screenshot used by regression / packaged tests.

No external assets, user screenshots, memory reading, or game control.
"""
from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont


def create_inventory_fixture(
    x=975, y=100, amount=50792, width=1920, height=1080
) -> Image.Image:
    img = Image.new("RGB", (width, height), (43, 48, 53))
    d = ImageDraw.Draw(img)
    # Sprite geometry matches the real client (850x900 reference crop).
    d.rectangle((x + 142, y, x + 727, y + 809),
                fill=(196, 214, 226), outline=(74, 107, 128), width=4)
    d.rectangle((x + 150, y + 6, x + 715, y + 59),
                fill=(240, 247, 248), outline=(107, 128, 144), width=3)
    d.rectangle((x + 152, y + 64, x + 715, y + 730),
                fill=(223, 219, 207), outline=(135, 145, 156), width=2)
    d.rectangle((x + 153, y + 733, x + 717, y + 806),
                fill=(154, 193, 218), outline=(87, 126, 152), width=2)
    d.rectangle((x + 210, y + 750, x + 620, y + 797),
                fill=(236, 245, 252), outline=(112, 142, 167), width=3)
    d.ellipse((x + 163, y + 758, x + 197, y + 791),
              fill=(243, 196, 23), outline=(142, 110, 20), width=3)
    try:
        title_font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 24)
        amount_font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 25)
        label_font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 21)
    except OSError:
        title_font = amount_font = label_font = ImageFont.load_default()
    d.text((x + 169, y + 21), "ITEM INVENTORY", font=title_font,
           fill=(45, 55, 65))
    digits = f"{amount:,}"
    bbox = d.textbbox((0, 0), digits, font=amount_font)
    d.text((x + 613 - (bbox[2] - bbox[0]), y + 752), digits,
           font=amount_font, fill=(40, 50, 60))
    d.text((x + 625, y + 756), "MESOS", font=label_font,
           fill=(236, 245, 253))
    return img
