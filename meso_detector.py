"""Automatic on-screen MapleStory inventory mesos detection.

Only recognizes pixels rendered in the game window. Never sends keystrokes,
reads game memory or inspects game network packets. The in-game inventory must
be visible for a new balance to be observable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from PIL import Image, ImageEnhance, ImageOps, ImageStat


@dataclass(frozen=True)
class MesoReading:
    amount: int
    # Relative rectangle containing the inventory's MESOS line, for fast
    # follow-up reads while the inventory stays in the same location.
    region: tuple[float, float, float, float]
    method: str
    confidence: int


# OCR can confuse 0/O, 5/S, and split "MESOS" into multiple tokens.
_LABEL = re.compile(r"(?:M\s*[E3F]\s*[S5]\s*[O0]\s*[S5]?|메\s*소)",
                    re.IGNORECASE)
_LAST_NUMBER = re.compile(r"(?<!\d)(?:\d{1,3}(?:[, .]\d{3})+|\d{1,10})(?!\d)")
_INVENTORY_TITLE = re.compile(r"ITEM\s+INVENTORY", re.IGNORECASE)


def _line_amount(text: str) -> int | None:
    normalized = text.replace("，", ",").replace("Ｏ", "O")
    match = _LABEL.search(normalized)
    if match is None:
        return None
    # The numeric balance precedes the label in MapleStory's inventory footer.
    # Only consider the nearest preceding digit group, never arbitrary HUD/chat
    # numbers that might appear elsewhere on the screen.
    prefix = normalized[: match.start()]
    groups = list(_LAST_NUMBER.finditer(prefix))
    if not groups:
        return None
    last = groups[-1]
    if prefix[last.end() :].strip(" :-|~") or len(prefix) > 42:
        return None
    # Chat/trade text such as 'Buy 30,000 mesos each item' must not be
    # mistaken for the inventory balance.
    if re.search(r"[A-Za-z가-힣]", prefix[: last.start()]):
        return None
    if re.search(r"[A-Za-z가-힣]", normalized[match.end() :]):
        return None
    amount = int(re.sub(r"\D", "", last.group()))
    return amount if 0 <= amount <= 9_999_999_999 else None


def _line_rect(line: dict) -> tuple[float, float, float, float] | None:
    words = line.get("words", [])
    bounds = [w.get("bounding_rect", {}) for w in words]
    bounds = [b for b in bounds if all(k in b for k in ("x", "y", "width", "height"))]
    if not bounds:
        return None
    return (
        min(b["x"] for b in bounds),
        min(b["y"] for b in bounds),
        max(b["x"] + b["width"] for b in bounds),
        max(b["y"] + b["height"] for b in bounds),
    )


def _region_for_line(rect, width, height):
    x1, y1, x2, y2 = rect
    # A few characters to the left of "MESOS" contain the number. Extend the
    # crop enough for 10-digit balances and keep the MESOS anchor in view.
    pad_x = max(100.0, (y2 - y1) * 12)
    pad_y = max(10.0, (y2 - y1) * 1.3)
    return (
        max(0.0, (x1 - pad_x) / width),
        max(0.0, (y1 - pad_y) / height),
        min(1.0, (x2 + 18) / width),
        min(1.0, (y2 + pad_y) / height),
    )


def _light_inventory_background(image: Image.Image, bounds) -> bool:
    """Reject dark-background chat text advertising a price in MESOS."""
    x1, y1, x2, y2 = bounds
    spread = max(6, int((y2-y1) * .4))
    # Take a strip around the actual observed text. Most pixels in the
    # MapleStory inventory footer are light blue/white.
    region = image.crop((
        max(0, int(x1)-spread),
        max(0, int(y1)-spread),
        min(image.width, int(x2)+spread),
        min(image.height, int(y2)+spread),
    ))
    if region.width < 10 or region.height < 10:
        return False
    return min(ImageStat.Stat(region.convert("RGB")).mean) >= 140


def _recognize(image: Image.Image) -> dict:
    from winocr import recognize_pil_sync
    import screen

    if screen._lang is None:
        screen.ocr_available()
    lang = screen._lang
    if lang is None:
        raise RuntimeError("Windows OCR 한국어/영어 언어팩이 필요합니다.")
    return recognize_pil_sync(image.convert("RGB"), lang=lang)


def _read_line_from_result(result: dict, image_size,
                           image: Image.Image | None = None) -> MesoReading | None:
    width, height = image_size
    for line in result.get("lines", []):
        raw = str(line.get("text", ""))
        amount = _line_amount(raw)
        if amount is None:
            continue
        bounds = _line_rect(line)
        if bounds is None:
            continue
        if image is not None and not _light_inventory_background(image, bounds):
            continue
        return MesoReading(
            amount, _region_for_line(bounds, width, height),
            "inventory_footer_mesos_label", 3,
        )
    return None


def _read_label_without_amount(result: dict, image_size):
    """Locate the MESOS label even if number and label were split by OCR."""
    width, height = image_size
    for line in result.get("lines", []):
        text = str(line.get("text", ""))
        if not _LABEL.search(text):
            continue
        rect = _line_rect(line)
        if rect:
            return _region_for_line(rect, width, height)
    return None


def _inventory_title_rect(result: dict):
    """Look for the actual inventory title, not arbitrary numbers or chat."""
    for line in result.get("lines", []):
        if _INVENTORY_TITLE.search(str(line.get("text", ""))):
            rect = _line_rect(line)
            if rect:
                return rect
    return None


def _real_mesos_label(crop: Image.Image) -> bool:
    """Require the 'MESOS' footer label, never an inventory item quantity."""
    if crop.width < 30 or crop.height < 12:
        return False
    variants = [crop]
    # Pixel-font labels sometimes show as 'ME505' / 'HE505' at 1x.
    variants.append(crop.resize(
        (crop.width * 2, crop.height * 2), Image.Resampling.NEAREST))
    for part in variants:
        for line in _recognize(part).get("lines", []):
            raw = re.sub(r"[^A-Za-z0-9]", "", str(line.get("text", ""))).upper()
            if _LABEL.search(raw) or re.fullmatch(
                r"(?:M|H|H1)[E3F][S5][O0][S5]", raw):
                return True
    return False


def _amount_from_number_only_crop(crop: Image.Image) -> int | None:
    """Read just the known inventory balance field, without relying on MESOS.

    Real Global Classic MapleStory OCR reliably sees '60,464' and 'MESOS'
    when cropped separately but often *never combines them into one line*.
    Only call this after the full-image OCR has located ITEM INVENTORY.
    """
    if crop.width < 35 or crop.height < 12:
        return None
    if min(ImageStat.Stat(crop.convert("RGB")).mean) < 125:
        return None
    # Prefer original-resolution OCR, which proved more reliable on the actual
    # 4K MapleStory inventory; use enlarged views only if needed.
    variants = [crop]
    for factor in (2, 3):
        variants.append(crop.resize((crop.width * factor, crop.height * factor),
                                    Image.Resampling.NEAREST))
    for image in variants:
        result = _recognize(image)
        for line in result.get("lines", []):
            raw = str(line.get("text", "")).strip()
            # Number-only narrow crop. Avoid guessed balances from junk text.
            if not re.fullmatch(r"[\[\(\{\s]*\d[\d,\. ]{0,13}[\]\)\}\s]*", raw):
                continue
            parts = re.findall(r"\d+", raw)
            if not parts:
                continue
            number = int("".join(parts))
            if 0 <= number <= 9_999_999_999:
                return number
    return None


def _read_from_inventory_title(frame: Image.Image, result: dict, scan_size,
                               scan_origin=(0, 0), source_size=None
                               ) -> MesoReading | None:
    """Read mesos from the footer whose position is anchored to the window title.

    The inventory moves freely around the screen. Offsets are scaled by the
    detected font height, avoiding any need for user region calibration.
    """
    title = _inventory_title_rect(result)
    if title is None:
        return None
    original_w, original_h = frame.size
    scan_w, scan_h = scan_size
    source_w, source_h = source_size or frame.size
    tx = scan_origin[0] + title[0] * source_w / scan_w
    ty = scan_origin[1] + title[1] * source_h / scan_h
    title_width = (title[2] - title[0]) * source_w / scan_w
    # The real pixel-font title is ~204 pixels wide at 3840x2160.
    # The OCR bounding-box HEIGHT is unreliable (11..16px for identical
    # UI); scaling by height previously sent the footer crop into the item
    # grid where quantities like "31" were mistaken for the MESOS balance.
    # The text WIDTH is much more stable, and scales correctly for smaller
    # game resolutions / different UI zoom levels.
    scale = max(.45, min(3.0, title_width / 204.0))
    box = (
        max(0, round(tx + 305 * scale)),
        max(0, round(ty + 708 * scale)),
        min(original_w, round(tx + 465 * scale)),
        min(original_h, round(ty + 780 * scale)),
    )
    if box[2] - box[0] < 35 or box[3] - box[1] < 12:
        return None
    # Confirm the actual MESOS label immediately to the right of the number.
    # No label = no balance. This specifically rejects the false 31-meso
    # reading reported from a 60,897-meso inventory.
    label_box = (
        max(0, round(tx + 445 * scale)),
        max(0, round(ty + 726 * scale)),
        min(original_w, round(tx + 555 * scale)),
        min(original_h, round(ty + 771 * scale)),
    )
    if (label_box[2] - label_box[0] < 30
        or label_box[3] - label_box[1] < 12
        or not _real_mesos_label(frame.crop(label_box))):
        return None
    crop = frame.crop(box)
    amount = _amount_from_number_only_crop(crop)
    if amount is None:
        return None
    return MesoReading(amount, (
        box[0] / original_w, box[1] / original_h,
        box[2] / original_w, box[3] / original_h,
    ), "inventory_title_footer_amount", 3)


def _crop_relative(img: Image.Image, region):
    w, h = img.size
    x1, y1, x2, y2 = region
    if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
        return None
    return img.crop((int(x1*w), int(y1*h), int(x2*w), int(y2*h)))


def _read_small_region(img: Image.Image, region) -> MesoReading | None:
    crop = _crop_relative(img, region)
    if crop is None or crop.width < 35 or crop.height < 10:
        return None
    # Pixelated game fonts need enlargement. Inverted/threshold images are
    # used only after the standard OCR pass fails.
    max_width = 1600
    factor = max(1, min(4, max_width // crop.width))
    scaled = crop.resize((crop.width*factor, crop.height*factor),
                         Image.Resampling.NEAREST)
    variants = (scaled,)
    try:
        gray = ImageOps.grayscale(scaled)
        variants += (
            ImageEnhance.Contrast(gray).enhance(1.7),
            gray.point(lambda x: 255 if x > 143 else 0),
        )
    except ValueError:
        pass
    for image in variants:
        result = _recognize(image)
        for line in result.get("lines", []):
            amount = _line_amount(str(line.get("text", "")))
            bounds = _line_rect(line)
            if amount is not None and bounds is not None and _light_inventory_background(image, bounds):
                return MesoReading(amount, tuple(region), "cached_mesos_footer", 3)
    return None


class AutoMesoDetector:
    """Finds inventory footer anywhere in the MapleStory game window."""

    def __init__(self):
        self.cached_region: tuple[float, float, float, float] | None = None
        self.missing_frames = 0
        self.cached_method: str | None = None

    def reset(self):
        self.cached_region = None
        self.missing_frames = 0
        self.cached_method = None

    def detect(self, frame: Image.Image) -> MesoReading | None:
        # Inventory can be moved anywhere. Never trust a cached numerical
        # crop or an isolated MESOS-looking substring without confirming the
        # inventory title AND the label in this frame. That could capture
        # stack counts when the inventory is reopened/moved/closed.
        width, height = frame.size
        # The real MapleStory inventory footer often OCRs the balance and
        # 'MESOS' as separate lines. Anchor the narrow balance field to the
        # uniquely recognizable 'ITEM INVENTORY' title instead.
        # Full 4K frame OCR often misses this tiny title entirely due to
        # distracting HUD/chat; use shorter half-screen passes. Always
        # revalidate the header instead of trusting stale coordinates.
        half = int(height * .56)
        # Start with upper/lower strips, then subdivide if the title is too
        # small against the busy MapleStory background.
        tiles = [
            (0, 0, width, half),
            (0, max(0, height-half), width, height),
            (0, 0, int(width * .60), half),
            (int(width * .40), 0, width, half),
            (0, max(0, height-half), int(width * .60), height),
            (int(width * .40), max(0, height-half), width, height),
        ]
        for left, top, right, bottom in tiles:
            section = frame.crop((left, top, right, bottom))
            ratio = min(1.0, 2400 / section.width)
            sc = section if ratio == 1.0 else section.resize(
                (round(section.width * ratio), round(section.height * ratio)),
                Image.Resampling.BILINEAR)
            title_result = _recognize(sc)
            if _inventory_title_rect(title_result) is None:
                continue
            fallback = _read_from_inventory_title(
                frame, title_result, sc.size, (left, top), section.size)
            if fallback is not None:
                self.cached_region = fallback.region
                self.cached_method = fallback.method
                self.missing_frames = 0
                return fallback
        self.missing_frames += 1
        if self.missing_frames >= 2:
            self.cached_region = None
            self.cached_method = None
        return None
