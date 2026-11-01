"""Read MapleStory's visible UI pixels. Never opens the game process or reads memory."""
from __future__ import annotations

import ctypes
import re
from ctypes import wintypes
from dataclasses import dataclass
from PIL import Image, ImageEnhance, ImageGrab, ImageOps

from engine import requirement

_user32 = ctypes.windll.user32
try:
    # Use physical screen pixel coordinates, not Windows virtualized DPI.
    _user32.SetProcessDPIAware()
except Exception:
    pass

DEFAULT_EXP_REGION = [0.515, 0.945, 0.642, 0.985]


@dataclass
class GameInfo:
    hwnd: int
    bbox: tuple[int, int, int, int]
    width: int
    height: int


def _find_window() -> int | None:
    exact = _user32.FindWindowW(None, "MapleStory")
    if exact and _user32.IsWindowVisible(exact):
        return exact
    found = []
    CALLBACK = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd, lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        size = _user32.GetWindowTextLengthW(hwnd)
        if size < 6:
            return True
        buffer = ctypes.create_unicode_buffer(size + 1)
        _user32.GetWindowTextW(hwnd, buffer, size + 1)
        if "MapleStory" in buffer.value:
            found.append(hwnd)
        return True

    _user32.EnumWindows(CALLBACK(visit), 0)
    return found[0] if found else None


def game_info() -> GameInfo | None:
    hwnd = _find_window()
    if not hwnd or _user32.IsIconic(hwnd):
        return None
    rect = wintypes.RECT()
    point = wintypes.POINT(0, 0)
    if not _user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    if not _user32.ClientToScreen(hwnd, ctypes.byref(point)):
        return None
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width < 480 or height < 300:
        return None
    return GameInfo(hwnd, (point.x, point.y, point.x + width, point.y + height),
                    width, height)


def grab_game(info: GameInfo) -> Image.Image:
    return ImageGrab.grab(bbox=info.bbox, all_screens=True)


def capture_region(info: GameInfo, region: list[float]) -> Image.Image | None:
    if not isinstance(region, (list, tuple)) or len(region) != 4:
        return None
    x1, y1, x2, y2 = (float(x) for x in region)
    if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
        return None
    left, top, _, _ = info.bbox
    bbox = (int(left + x1 * info.width), int(top + y1 * info.height),
            int(left + x2 * info.width), int(top + y2 * info.height))
    if bbox[2] - bbox[0] < 15 or bbox[3] - bbox[1] < 8:
        return None
    return ImageGrab.grab(bbox=bbox, all_screens=True)


def crop_game_region(frame: Image.Image, region: list[float]) -> Image.Image | None:
    """Crop normalized game coordinates from one consistent captured frame."""
    if not isinstance(region, (list, tuple)) or len(region) != 4:
        return None
    try:
        x1, y1, x2, y2 = (float(x) for x in region)
    except (TypeError, ValueError):
        return None
    if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
        return None
    w, h = frame.size
    box = (int(x1 * w), int(y1 * h), int(x2 * w), int(y2 * h))
    if box[2] - box[0] < 15 or box[3] - box[1] < 8:
        return None
    return frame.crop(box)


def _engines():
    from winrt.windows.globalization import Language
    from winrt.windows.media.ocr import OcrEngine
    for lang in ("ko", "en", "en-US"):
        try:
            if OcrEngine.is_language_supported(Language(lang)):
                return lang
        except Exception:
            continue
    return None


_lang: str | None = None


def ocr_available() -> bool:
    global _lang
    _lang = _engines()
    return bool(_lang)


def recognize(img: Image.Image) -> str:
    global _lang
    if _lang is None:
        _lang = _engines()
    if _lang is None:
        raise RuntimeError("Windows 문자 인식 언어팩이 없습니다. 한국어 또는 영어 언어팩이 필요합니다.")
    from winocr import recognize_pil_sync
    result = recognize_pil_sync(img, lang=_lang)
    return str(result.get("text", "")).strip()


def _variants(img: Image.Image):
    # Game font is small, antialiased, and outlined. Binary contrast is
    # often substantially more accurate than color OCR for digits.
    if img.width > 1000:
        scale = max(1, min(3, 1800 // img.width))
    else:
        scale = max(2, min(5, 1850 // img.width))
    resized = img.resize((img.width * scale, img.height * scale),
                         Image.Resampling.NEAREST)
    gray = ImageOps.grayscale(resized)
    yield "bw", gray.point(lambda x: 255 if x > 138 else 0)
    yield "gray", ImageEnhance.Contrast(gray).enhance(1.8)
    yield "color", resized


_EXP_PATTERN = re.compile(
    # Keep digits separated by whitespace from being merged into EXP.
    # E.g. OCR '0(2 52,845[96.26/+]' must read EXP=52,845, not 252,845.
    r"(?<!\d)(\d{1,3}(?:\s*[,\.]\s*\d{3})+|\d{1,10})\s*[\[\(\{]?\s*(\d{1,3})\s*(?:[.,*]|\s)\s*(\d{1,2})\s*(?:[%％]|[/\\]\+?)",
    re.IGNORECASE)


def parse_exp_text(text: str) -> tuple[int, float] | None:
    text = text.replace("，", ",").replace("O", "0")
    candidates = []
    for found in _EXP_PATTERN.finditer(text):
        exp_str = re.sub(r"[^0-9]", "", found.group(1))
        if not exp_str.isdigit():
            continue
        exp = int(exp_str)
        percentage = float(found.group(2) + "." + found.group(3))
        if 0 <= percentage <= 100 and exp >= 0:
            candidates.append((exp, percentage))
    return candidates[0] if candidates else None


_EXP_AMOUNT_TOKEN = re.compile(
    r"(?<!\d)(?:\d{1,3}(?:[,\.]\s*\d{3})+|\d{1,7})(?!\d)")


def _screen_exp_candidates(text: str, level: int) -> list[tuple[int, float | None]]:
    """Recover EXP when Windows OCR splits digits or mangles '%' characters.

    E.g. real game pixels:
      '7,514 [1 1 *80%]' -> 7,514 / 11.80%
      '�27,514[11 80/+1' -> 7,514 / 11.80% (spurious prefix)
      'EXP 7,514 (1 1.80+%1' -> 7,514 / 11.80%
    Verify each candidate against EXP required for the displayed level.
    """
    clean = text.replace("，", ",").replace("O", "0").replace("o", "0")
    left, separator, right = re.split(r"([\[\(\{])", clean, maxsplit=1) if re.search(r"[\[\(\{]", clean) else (clean, "", "")
    # Omitted, badly recognized bracket? The first part still contains an
    # EXP number, but only accept it with a second OCR vote or recent value.
    suffix = right.split("%", 1)[0].split("/", 1)[0].split("+", 1)[0]
    pct_digits = re.sub(r"[^0-9]", "", suffix[:11])
    # Two digits without the separator are ambiguous, e.g. OCR
    # '1 13,543 [31 +38+41' is NOT evidence of 0.31% experience.
    pct = float(int(pct_digits) / 100) if 3 <= len(pct_digits) <= 5 else None
    if pct is not None and not 0 <= pct <= 100:
        pct = None
    amounts = []
    for m in _EXP_AMOUNT_TOKEN.finditer(left):
        digits = re.sub(r"\D", "", m.group())
        if not digits:
            continue
        amounts.append(int(digits))
        if len(digits) >= 5:
            # OCR sometimes incorporates '2' from the EXP label before a
            # comma, turning 7,514 into 27,514. The percent test decides.
            amounts.append(int(digits[1:]))
        if len(digits) >= 6:
            amounts.append(int(digits[2:]))
    # Take closest token to the EXP percentage, not HUD digits elsewhere.
    amounts = list(dict.fromkeys(amounts[-6:]))
    return [(amount, pct) for amount in amounts if amount >= 0]


def read_exp(img: Image.Image, level: int, last_exp: int | None = None
             ) -> tuple[int, float, str] | None:
    matches: list[tuple[float, int, float, str]] = []
    req = requirement(level)
    observed_without_pct: dict[int, int] = {}
    for variant, prepared in _variants(img):
        try:
            text = recognize(prepared)
        except Exception:
            continue
        parsed = parse_exp_text(text)
        candidates = _screen_exp_candidates(text, level)
        if parsed is not None:
            candidates.insert(0, parsed)
        # Small outlined digits are sometimes separated by a spurious space:
        # '1 9,497[35.51%]' actually means 19,497. Only accept a merged
        # version when it agrees with the game's EXP percentage.
        split = re.search(
            r"(?<!\d)(\d)\s+(\d{1,3}(?:\s*[,\.]\s*\d{3})+)\s*"
            r"[\[\(\{]?\s*(\d{1,3})\s*[.,]\s*(\d{1,2})\s*"
            r"(?:[%％]|[/\\]\+?)", text)
        if split:
            joined = int(split.group(1) + re.sub(r"\D", "", split.group(2)))
            candidates.append((joined, float(split.group(3) + "." + split.group(4))))
        candidates = list(dict.fromkeys(candidates))
        for exp, pct in candidates:
            if req and exp >= req:
                continue
            if req:
                if pct is None:
                    # Amount-only OCR is unsafe to use as the initial session
                    # baseline. In real gameplay, a tightly cropped '113,543'
                    # was repeatedly misread as '1' by *multiple* variants.
                    # Permit this fallback only near the already-known EXP.
                    if last_exp is None or abs(exp - last_exp) > max(2000, int(req * .02)):
                        continue
                    # Only allow amount-only OCR if it has strong corroboration.
                    observed_without_pct[exp] = observed_without_pct.get(exp, 0) + 1
                    if observed_without_pct[exp] < 2:
                        continue
                    discrepancy = 0.12
                    pct = round(exp / req * 100, 2)
                else:
                    discrepancy = abs(exp / req * 100 - pct)
                    # A real percentage rounded to 2 decimals agrees within
                    # ~0.005%; 0.55% admitted '1 EXP / 0.31%' false positives.
                    if discrepancy > 0.18:
                        continue
            else:
                discrepancy = 0.0
                if pct is None:
                    continue
            similarity = abs(exp - last_exp) / max(req or 10000, 1) if last_exp else 0.0
            score = discrepancy + similarity * 0.06 + (0.03 if variant == "color" else 0)
            matches.append((score, exp, pct, text))
            # Most real in-game scans have a clear black/white reading.
            # Skip two extra WinRT OCR calls when the first result agrees
            # closely with the level's EXP table. Ambiguous scans still
            # examine all variants as before.
            if variant == "bw" and discrepancy < 0.20 and similarity < 0.2:
                return exp, pct, text
    if not matches:
        return None
    matches.sort()
    _, exp, pct, text = matches[0]
    return exp, pct, text


_NUMBER = re.compile(r"(?<![A-Za-z0-9])\d{1,3}(?:[, .]\d{3})+|(?<![A-Za-z0-9])\d{2,10}(?![A-Za-z0-9])")


def parse_meso_text(text: str) -> int | None:
    raw = re.sub(r"\d+[.,]\d+\s*%", "", text)
    raw = raw.replace("，", ",").replace("₩", "").replace("Ⓜ", "")
    matches = _NUMBER.findall(raw)
    if not matches:
        return None
    numbers = [int(re.sub(r"[^0-9]", "", x)) for x in matches]
    # A small user-selected ROI should only cover the balance label/value.
    return max(numbers) if numbers else None


def read_meso(img: Image.Image) -> tuple[int, str] | None:
    answers: list[tuple[int, str]] = []
    for variant, prepared in _variants(img):
        try:
            text = recognize(prepared)
        except Exception:
            continue
        meso = parse_meso_text(text)
        if meso is not None:
            answers.append((meso, text))
    if not answers:
        return None
    # Prefer consensus, falling back to the binary-contrast OCR candidate.
    from collections import Counter
    votes = Counter(value for value, _ in answers)
    best = votes.most_common(1)[0][0]
    return next(x for x in answers if x[0] == best)
