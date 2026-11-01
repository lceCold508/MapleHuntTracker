"""Vision-only detection of the MapleStory Classic Haste buff icon.

The template was captured from the actual game buff bar (not the skill menu),
and only the icon's stable center is included. The matcher searches the
top-right game HUD, so the icon may move when other buffs are applied.

No game input, memory access, network traffic or process injection is used.
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image


# The reference comes from a 3840x2160 game screenshot. Its crop is 50x50.
REFERENCE_HEIGHT = 2160
DEFAULT_MATCH_THRESHOLD = 0.87
PRESENT_CONFIRM_FRAMES = 2
# Haste shows a yellow countdown over the *same* buff icon from 56s remaining.
# That number is not a disappearance: an uncovered sprite-only comparison
# loses the match, resulting in the v1.9 false alarm. The 3-second missing
# interval observed during diagnostics was the player manually removing and
# reapplying Haste, not an animation. Keep the alert prompt once it truly goes
# away, with short evidence-based debounce rather than the interim 8s delay.
# Screen capture and icon animations can occasionally produce one to three
# consecutive low-confidence frames. Do not show buff loss or beep until the
# icon has actually been absent for around one second.
ABSENT_CONFIRM_FRAMES = 4
ABSENT_CONFIRM_SECONDS = 0.82
LOCAL_CONFIRM_THRESHOLD = 0.82
LOCAL_CONFIRM_COLOR_THRESHOLD = 0.955
# The countdown overlays the center/lower 2/3 of the reference icon. Match
# the unoccluded border/upper sprite separately as a fallback.
NUMBER_MASKED_CORR_THRESHOLD = 0.94
NUMBER_MASKED_COEFF_THRESHOLD = 0.78
# A colored 1..56 second number is independent evidence that the last known
# Haste slot still has a countdown, even if all global template matches fail.
COUNTDOWN_MIN_YELLOW_PIXELS = 12
COUNTDOWN_BORDER_COEFF_THRESHOLD = 0.42
REARM_COOLDOWN_SECONDS = 5.0
BUFF_REGION = (0.60, 0.0, 1.0, 0.12)


def bundled_template_path() -> Path:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return root / "assets" / "haste_buff_template.png"


@dataclass(frozen=True)
class MatchResult:
    visible: bool
    score: float
    position: tuple[int, int] | None


class HasteMatcher:
    """Scale-aware normalized color-template matcher for HUD screenshots."""

    def __init__(self, template_path: Path | None = None,
                 threshold: float = DEFAULT_MATCH_THRESHOLD):
        self.template_path = template_path or bundled_template_path()
        self.threshold = float(threshold)
        reference = Image.open(self.template_path).convert("RGB")
        self.ref = cv2.cvtColor(np.asarray(reference), cv2.COLOR_RGB2BGR)
        self._scaled: dict[tuple[int, int], np.ndarray] = {}
        self._scaled_masks: dict[tuple[int, int], np.ndarray] = {}
        h, w = self.ref.shape[:2]
        self._number_mask = np.full((h, w), 255, dtype=np.uint8)
        # Exclude digits + their thick black outline (conservatively). Keep
        # the Haste-shaped sprite at the top and surrounding icon border.
        self._number_mask[int(.28*h):, int(.10*w):int(.96*w)] = 0

    def _variants(self, frame_height: int) -> list[np.ndarray]:
        scale = max(0.25, min(2.5, frame_height / REFERENCE_HEIGHT))
        w, h = self.ref.shape[1], self.ref.shape[0]
        sizes = set()
        for adjustment in (0.96, 1.0, 1.04):
            sizes.add((max(11, int(round(w * scale * adjustment))),
                       max(11, int(round(h * scale * adjustment)))))
        result = []
        for size in sorted(sizes):
            if size not in self._scaled:
                self._scaled[size] = cv2.resize(
                    self.ref, size,
                    interpolation=(cv2.INTER_AREA if size[0] < w else cv2.INTER_LINEAR))
                self._scaled_masks[size] = cv2.resize(
                    self._number_mask, size, interpolation=cv2.INTER_NEAREST)
            result.append(self._scaled[size])
        return result

    @staticmethod
    def _safe_masked_match(image: np.ndarray, template: np.ndarray,
                           mask: np.ndarray, method: int) -> np.ndarray | None:
        """A masked OpenCV match may fail on transient frames / driver paths.

        Never let one OpenCV exception kill or permanently disable the Haste
        watcher. Skip the problematic candidate and try the next frame.
        """
        ih, iw = image.shape[:2]
        th, tw = template.shape[:2]
        if ih < th or iw < tw or mask.shape[:2] != (th, tw):
            return None
        try:
            result = cv2.matchTemplate(image, template, method, mask=mask)
        except cv2.error:
            # The recurring OpenCV 5 matchTemplateMask assertion can also
            # occur for an otherwise valid same-size 50x50 candidate patch.
            # Calculate its masked similarity directly with NumPy so the
            # known Haste countdown remains detectable instead of dropping
            # the icon for one or more seconds.
            if (ih, iw) != (th, tw) or method not in (
                    cv2.TM_CCORR_NORMED, cv2.TM_CCOEFF_NORMED):
                return None
            enabled = mask > 0
            if not np.any(enabled):
                return None
            original = image[enabled].astype(np.float64).reshape(-1)
            reference = template[enabled].astype(np.float64).reshape(-1)
            if method == cv2.TM_CCOEFF_NORMED:
                original -= original.mean()
                reference -= reference.mean()
            denominator = float(np.linalg.norm(original) * np.linalg.norm(reference))
            if denominator < 1e-12:
                return None
            score = float(np.clip(np.dot(original, reference) / denominator, -1, 1))
            return np.array([[score]], dtype=np.float32)
        return result if result is not None and result.size else None

    def _countdown_at_position(self, frame: np.ndarray, full_frame_height: int,
                               position: tuple[int, int] | None) -> MatchResult | None:
        """Keep Haste active when yellow countdown covers its last icon slot.

        This check is anchored to a *confirmed* Haste position and is not a
        generic search for yellow numbers (which could be other game UI).
        Require both yellow digit pixels and surviving Haste icon border.
        """
        if position is None:
            return None
        scale = max(0.25, min(2.5, full_frame_height / REFERENCE_HEIGHT))
        tolerance = max(1, round(3 * scale))
        x0, y0 = position
        required_yellow = max(3, round(COUNTDOWN_MIN_YELLOW_PIXELS * scale * scale))
        for template in self._variants(full_frame_height):
            height, width = template.shape[:2]
            mask = self._scaled_masks[(width, height)]
            for dy in (0, -tolerance, tolerance):
                for dx in (0, -tolerance, tolerance):
                    x, y = x0 + dx, y0 + dy
                    if (x < 0 or y < 0 or x + width > frame.shape[1] or
                            y + height > frame.shape[0]):
                        continue
                    patch = frame[y:y + height, x:x + width]
                    # BGR format; yellow digits have high R/G and little B.
                    b, g, r = (patch[:, :, 0], patch[:, :, 1], patch[:, :, 2])
                    yellow = ((r.astype(np.int16) > 170) & (g.astype(np.int16) > 120)
                              & (b.astype(np.int16) < 130)
                              & (r.astype(np.int16) > b.astype(np.int16) * 1.6))
                    if np.count_nonzero(yellow) < required_yellow:
                        continue
                    matched = self._safe_masked_match(
                        patch, template, mask, cv2.TM_CCOEFF_NORMED)
                    if matched is None:
                        continue
                    coeff = float(matched[0, 0])
                    if np.isfinite(coeff) and coeff >= COUNTDOWN_BORDER_COEFF_THRESHOLD:
                        return MatchResult(True, min(1.0, max(0.87, coeff)), (x, y))
        return None

    def _known_slot_match(self, frame: np.ndarray, full_frame_height: int,
                          position: tuple[int, int] | None) -> MatchResult | None:
        """Fast, stable detection around an already-confirmed Haste HUD slot.

        Rechecking the known slot before doing a full HUD search avoids
        confusing unrelated status icons and the entire screen during brief
        yellow-number animation frames. The borderline threshold is only
        accepted when the candidate has strong color similarity too.
        """
        if position is None:
            return None
        tolerance = max(2, round(4 * full_frame_height / REFERENCE_HEIGHT))
        x0, y0 = position
        best = (-1.0, None)
        for template in self._variants(full_frame_height):
            height, width = template.shape[:2]
            for dy in (0, -tolerance, tolerance):
                for dx in (0, -tolerance, tolerance):
                    x, y = x0 + dx, y0 + dy
                    if (x < 0 or y < 0 or x + width > frame.shape[1]
                            or y + height > frame.shape[0]):
                        continue
                    patch = frame[y:y + height, x:x + width]
                    coeff = float(cv2.matchTemplate(
                        patch, template, cv2.TM_CCOEFF_NORMED)[0, 0])
                    if not np.isfinite(coeff) or coeff < LOCAL_CONFIRM_THRESHOLD:
                        continue
                    if coeff < self.threshold:
                        corr = float(cv2.matchTemplate(
                            patch, template, cv2.TM_CCORR_NORMED)[0, 0])
                        if not np.isfinite(corr) or corr < LOCAL_CONFIRM_COLOR_THRESHOLD:
                            continue
                    if coeff > best[0]:
                        best = (coeff, (x, y))
        if best[1] is not None:
            return MatchResult(True, best[0], best[1])
        return None

    def match(self, image: Image.Image, full_frame_height: int,
              last_position: tuple[int, int] | None = None) -> MatchResult:
        if image.width < 70 or image.height < 30:
            return MatchResult(False, 0.0, None)
        frame = cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2BGR)
        local = self._known_slot_match(frame, full_frame_height, last_position)
        if local is not None:
            return local
        best_score = -1.0
        best_point = None
        for template in self._variants(full_frame_height):
            if frame.shape[0] < template.shape[0] or frame.shape[1] < template.shape[1]:
                continue
            scores = cv2.matchTemplate(frame, template, cv2.TM_CCOEFF_NORMED)
            _, score, _, location = cv2.minMaxLoc(scores)
            if score > best_score:
                best_score = float(score)
                best_point = (int(location[0]), int(location[1]))
        if best_score >= self.threshold:
            return MatchResult(True, best_score, best_point)

        # During countdown the number obscures the actual Haste sprite. Use
        # a pixel mask to ignore the number, and require BOTH color similarity
        # and correlated detail so arbitrary blue/gray buff icon frames are
        # not incorrectly mistaken for Haste.
        masked_score = 0.0
        masked_point = None
        for template in self._variants(full_frame_height):
            h, w = template.shape[:2]
            if frame.shape[0] < h or frame.shape[1] < w:
                continue
            mask = self._scaled_masks[(w, h)]
            color_score = self._safe_masked_match(
                frame, template, mask, cv2.TM_CCORR_NORMED)
            if color_score is None:
                continue
            _, corr, _, position = cv2.minMaxLoc(color_score)
            if corr < NUMBER_MASKED_CORR_THRESHOLD:
                continue
            x, y = position
            candidate = frame[y:y+h, x:x+w]
            matched = self._safe_masked_match(
                candidate, template, mask, cv2.TM_CCOEFF_NORMED)
            if matched is None:
                continue
            coeff = float(matched[0, 0])
            if (np.isfinite(coeff) and coeff >= NUMBER_MASKED_COEFF_THRESHOLD
                    and corr > masked_score):
                masked_score = float(corr)
                masked_point = (int(x), int(y))
        if masked_point is not None:
            return MatchResult(True, masked_score, masked_point)
        countdown = self._countdown_at_position(frame, full_frame_height, last_position)
        if countdown is not None:
            return countdown
        return MatchResult(False, max(0.0, best_score), best_point)


class HasteState:
    """Alert after consistent absence of Haste, including the countdown state.

    When the game stops providing frames, update() is *not* called. This
    distinguishes a buff disappearing from the game being minimized, stale,
    frozen, or a Windows Graphics Capture interruption. The detector returns
    visible=True for a countdown-number-covered icon, so it remains armed.
    """

    def __init__(self):
        self.state = "waiting"  # waiting | active | checking | expired
        self.visible_count = 0
        self.missing_count = 0
        self.last_alert_at = -1e9
        self.last_score = 0.0
        self.last_visible_at: float | None = None
        self.last_position: tuple[int, int] | None = None
        self.last_observed_at: float | None = None

    def suspend_capture(self) -> None:
        """Clear a half-confirmed disappearance after WGC source disruption."""
        self.missing_count = 0
        if self.state == "checking":
            self.state = "active"

    def update(self, match: MatchResult, now: float | None = None) -> bool:
        """Returns True exactly once per confirmed active->expired transition."""
        now = time.monotonic() if now is None else now
        if self.last_observed_at is not None and now <= self.last_observed_at:
            return False  # stale capture is never evidence of expiration
        self.last_observed_at = now
        self.last_score = match.score
        if match.visible:
            was_armed = self.state in ("active", "checking")
            self.visible_count = (PRESENT_CONFIRM_FRAMES if was_armed else
                                  self.visible_count + 1)
            self.missing_count = 0
            self.last_visible_at = now
            if match.position is not None:
                self.last_position = match.position
            if was_armed or self.visible_count >= PRESENT_CONFIRM_FRAMES:
                self.state = "active"
            return False

        self.visible_count = 0
        if self.state not in ("active", "checking"):
            # No noise when Haste was never seen, nor repeated alerts after
            # it disappears. A new buff must be confirmed before rearming.
            self.missing_count = 0
            return False

        self.missing_count += 1
        self.state = "checking"  # temporary loss: require more fresh frames
        if (self.missing_count < ABSENT_CONFIRM_FRAMES or
                self.last_visible_at is None or
                now - self.last_visible_at < ABSENT_CONFIRM_SECONDS):
            return False
        self.state = "expired"
        self.missing_count = 0
        if now - self.last_alert_at < REARM_COOLDOWN_SECONDS:
            return False
        self.last_alert_at = now
        return True


def beep_haste_expired() -> None:
    """Two non-music alert tones: a recognizable short 'beep-beep'."""
    try:
        import winsound
        winsound.Beep(1050, 140)
        time.sleep(0.08)
        winsound.Beep(1600, 170)
    except (OSError, RuntimeError):
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except (OSError, RuntimeError):
            pass
