"""Haste countdown-number matching and correct actual buff expiry QA."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont

from haste_buff import (HasteMatcher, HasteState, MatchResult,
                        beep_haste_expired, DEFAULT_MATCH_THRESHOLD,
                        ABSENT_CONFIRM_SECONDS)


def check_matching():
    matcher = HasteMatcher()
    template = Image.open(matcher.template_path).convert("RGB")
    region = Image.new("RGB", (1536, 367), (31, 58, 67))
    region.paste(template, (1200, 25))
    match = matcher.match(region, 2160)
    assert match.visible and match.score > 0.97, match
    assert match.position == (1200, 25), match

    # Other buffs in the same bar should not prevent matching Haste after its
    # slot moves left; the detector never depends on the rightmost position.
    region.paste(Image.new("RGB", template.size, (105, 55, 50)), (1200, 25))
    region.paste(template, (600, 60))
    shifted = matcher.match(region, 2160)
    assert shifted.visible and shifted.score > 0.97 and shifted.position == (600, 60), shifted
    region.paste(Image.new("RGB", template.size, (105, 55, 50)), (600, 60))
    none = matcher.match(region, 2160)
    assert not none.visible and none.score < DEFAULT_MATCH_THRESHOLD, none

    # 1280x720 / scaled window: 50x50 buff sprite becomes about 17x17.
    scaled = Image.new("RGB", (512, 122), (20, 30, 50))
    icon = template.resize((17, 17), Image.Resampling.AREA if hasattr(Image.Resampling, "AREA") else Image.Resampling.BOX)
    scaled.paste(icon, (360, 7))
    small = matcher.match(scaled, 720)
    assert small.visible and small.score > 0.87, small
    print("HASTE_MATCH_PASS", round(match.score, 3), round(small.score, 3))


def check_real_countdown_icon():
    """The live yellow countdown (40, 6, 5 seconds) must still be Haste.

    v1.9 incorrectly treated these numeric overlays as no buff; it then
    beeped with ~56 seconds left. These screenshots are from the real client.
    """
    matcher = HasteMatcher()
    folder = Path(__file__).resolve().parent / "assets" / "qa_fixtures"
    for name in ("haste-top-right-now.png", "haste-countdown-sample-0.png",
                 "haste-countdown-sample-1.png", "haste-countdown-sample-2.png"):
        screenshot = Image.open(folder / name).convert("RGB")
        match = matcher.match(screenshot, 2160)
        assert match.visible, (name, match)
        assert match.score >= .94, (name, match)
        # Don't mistake a random background patch for the Haste HUD.
        assert match.position is not None and match.position[1] == 20, (name, match)
        print("COUNTDOWN_MATCH", name, round(match.score, 3))

    # Replacing just the buff icon yields a genuinely missing icon.
    from PIL import ImageDraw
    screenshot = Image.open(folder / "haste-top-right-now.png").convert("RGB")
    draw = ImageDraw.Draw(screenshot)
    draw.rectangle((235, 0, screenshot.width, 115), fill=(48, 90, 60))
    absent = matcher.match(screenshot, 2160)
    assert not absent.visible, absent
    print("HASTE_COUNTDOWN_REGRESSION_PASS")


def check_countdown_forced_fallback():
    """Test the last-known-slot yellow-digit fallback, not just main matcher.

    Artificially make both global match branches fail, then confirm real
    remaining-time digit overlays still give a positive result at the
    already-established Haste slot. No-digit empty background must not.
    """
    folder = Path(__file__).resolve().parent / "assets" / "qa_fixtures"
    for name in ("haste-top-right-now.png", "haste-countdown-sample-0.png",
                 "haste-countdown-sample-1.png"):
        frame = Image.open(folder / name).convert("RGB")
        matcher = HasteMatcher(threshold=1.1)
        # Probe with globally strong match to determine verified icon slot.
        normal = HasteMatcher().match(frame, 2160)
        assert normal.visible and normal.position is not None, name
        with patch("haste_buff.NUMBER_MASKED_CORR_THRESHOLD", 1.1):
            rescued = matcher.match(frame, 2160, last_position=normal.position)
            assert rescued.visible, (name, rescued)
            assert rescued.position == normal.position, (name, rescued)
        print("COUNTDOWN_FALLBACK_PASS", name)

    # The reported ~30-second failure must also retain the previously
    # confirmed Haste slot, including a two-digit yellow countdown.
    icon = Image.open(matcher.template_path).convert("RGB")
    for digits in ("30", "56"):
        frame = Image.new("RGB", (110, 135), (44, 81, 59))
        drawn = icon.copy()
        try:
            font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 28)
        except OSError:
            font = ImageFont.load_default()
        ImageDraw.Draw(drawn).text((8, 18), digits, font=font,
                                  fill=(255, 232, 0), stroke_width=2,
                                  stroke_fill=(0, 0, 0))
        frame.paste(drawn, (32, 20))
        matcher = HasteMatcher(threshold=1.1)
        with patch("haste_buff.NUMBER_MASKED_CORR_THRESHOLD", 1.1):
            rescued = matcher.match(frame, 2160, last_position=(32, 20))
        assert rescued.visible, (digits, rescued)
        print("DIGIT_ANNOTATION_KEEPALIVE_PASS", digits)

    # Yellow text alone is never enough without the original Haste border.
    bad = Image.new("RGB", (110, 135), (44, 81, 59))
    ImageDraw.Draw(bad).text((35, 35), "30", fill=(255, 255, 0))
    matcher = HasteMatcher(threshold=1.1)
    with patch("haste_buff.NUMBER_MASKED_CORR_THRESHOLD", 1.1):
        rejected = matcher.match(bad, 2160, last_position=(32, 20))
    assert not rejected.visible, rejected
    print("UNRELATED_YELLOW_NUMBER_REJECTED")


def check_transitions():
    yes = MatchResult(True, 0.96, (1458, 20))
    no = MatchResult(False, 0.41, None)
    state = HasteState()
    assert not any(state.update(no, float(index)) for index in range(10))
    assert state.state == "waiting"
    assert not state.update(yes, 10.0)
    assert state.state == "waiting"
    assert not state.update(yes, 10.2)
    assert state.state == "active"

    assert not state.update(no, 10.4)
    assert state.state == "checking"
    assert not state.update(no, 10.4)  # cached screenshot is not new evidence
    assert state.state == "checking"
    assert not state.update(yes, 10.6)
    assert state.state == "active"

    # 3 fresh missing frames and 0.38s since the last positive.
    assert not state.update(no, 10.8)
    assert state.state == "checking"
    assert not state.update(no, 11.02)
    assert state.update(no, 11.24)
    assert state.state == "expired"
    assert not state.update(no, 11.3)
    assert not state.update(yes, 17.0)
    assert not state.update(yes, 17.2)
    assert state.state == "active"  # new buff re-arms automatically
    assert not state.update(no, 17.4)
    assert not state.update(no, 17.62)
    assert state.update(no, 17.84)
    assert state.state == "expired"
    assert ABSENT_CONFIRM_SECONDS == 0.38
    print("HASTE_TRANSITIONS_PASS")


def check_fast_alarm_latency():
    """At 200ms capture cadence, real loss alerts within about 0.6-0.8s."""
    visible = MatchResult(True, .97, (1458, 20))
    missing = MatchResult(False, .43, None)
    monitor = HasteState()
    for moment in (100.0, 100.2, 100.4):
        assert not monitor.update(visible, moment)
    # Countdown icon remains visible, so no early alarm.
    for moment in (100.6, 100.8):
        assert not monitor.update(MatchResult(True, .96, (1458, 20)), moment)
    assert monitor.state == "active"
    disappeared_at = 101.0
    assert not monitor.update(missing, disappeared_at)
    assert not monitor.update(missing, 101.2)
    alert_at = 101.4
    assert monitor.update(missing, alert_at)
    assert alert_at - disappeared_at < .6
    assert not monitor.update(missing, 101.6)
    print("FAST_HASTE_ALERT_PASS", round(alert_at - disappeared_at, 2))


def check_real_manual_recast():
    """Replay the interval when the user manually turned Haste off and on.

    The icon really was removed between about t=4s and t=7.39s. The detected
    alarm is legitimate for actual buff removal; it must re-arm after recast.
    """
    samples = [
        (0.00, .967, (1458, 20)), (.67, .967, (1458, 20)),
        (1.34, .966, (1458, 20)), (2.02, .974, (1458, 20)),
        (2.69, .968, (1458, 20)), (3.36, .975, (1458, 20)),
        (4.03, .768, (1458, 20)), (4.70, .397, (968, 313)),
        (5.38, .397, (971, 313)), (6.05, .397, (968, 313)),
        (6.72, .454, (1488, 227)), (7.39, .974, (1458, 20)),
        (8.06, .973, (1458, 20)), (8.74, .973, (1458, 20)),
        (9.41, .979, (1458, 20)), (10.08, .979, (1458, 20)),
        (10.77, .978, (1458, 20)), (11.44, .980, (1458, 20)),
        (12.11, .980, (1458, 20)), (12.78, .979, (1458, 20)),
        (13.45, .976, (1458, 20)), (14.12, .980, (1458, 20)),
        (14.80, .980, (1458, 20)), (15.47, .979, (1458, 20)),
        (16.14, .975, (1458, 20)), (16.81, .975, (1458, 20)),
        (17.49, .978, (1458, 20)),
    ]
    model = HasteState()
    alerts = []
    for stamp, score, point in samples:
        sample = MatchResult(score >= DEFAULT_MATCH_THRESHOLD, score, point)
        if model.update(sample, now=stamp):
            alerts.append(stamp)
    assert len(alerts) == 1, f"manual removal must trigger exactly once: {alerts}"
    assert model.state == "active"
    print("HASTE_MANUAL_REMOVE_RECAST_PASS", len(samples))


def check_beep():
    with patch("winsound.Beep") as beep, patch("haste_buff.time.sleep"):
        beep_haste_expired()
    assert beep.call_count == 2
    assert beep.call_args_list[0].args == (1050, 140)
    assert beep.call_args_list[1].args == (1600, 170)
    print("HASTE_BEEP_PASS")


if __name__ == "__main__":
    check_matching()
    check_real_countdown_icon()
    check_countdown_forced_fallback()
    check_transitions()
    check_fast_alarm_latency()
    check_real_manual_recast()
    check_beep()
