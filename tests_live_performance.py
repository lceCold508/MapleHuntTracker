"""Read-only, short local-game EXP OCR benchmark for v1.2.

Does not touch game process memory, keys, network, or session DB.
"""
from __future__ import annotations

import time

from app import EXP_POLL_INTERVAL, load_config
from screen import capture_region, game_info, read_exp


def main():
    info = game_info()
    if info is None:
        print("GAME_ABSENT live benchmark skipped")
        return
    conf = load_config()
    roi = conf["exp_roi"]
    configured_level = int(conf["level"])
    # The user may have leveled up since saving tracker settings; find a
    # plausible level without altering their saved configuration.
    level = configured_level
    for candidate_level in range(max(1, level-1), min(60, level+4)):
        preview = capture_region(info, roi)
        if preview is not None and read_exp(preview, candidate_level):
            level = candidate_level
            break
    durations = []
    values = []
    last_exp = None
    for _ in range(8):
        start = time.perf_counter()
        image = capture_region(info, roi)
        reading = read_exp(image, level, last_exp) if image else None
        duration = time.perf_counter() - start
        durations.append(duration)
        values.append(reading[:2] if reading else None)
        if reading:
            last_exp = reading[0]
        time.sleep(max(0, EXP_POLL_INTERVAL - duration))
    print("GAME_CLIENT", f"{info.width}x{info.height}",
          "CONFIG_LEVEL", configured_level, "CHECKED_LEVEL", level)
    print("LIVE_EXP", values)
    print("SCAN_MS", [round(t * 1000) for t in durations])
    print("AVERAGE_SCAN_MS", round(sum(durations) / len(durations) * 1000))
    print("READS_VALID", sum(v is not None for v in values), "OUT_OF", len(values))


if __name__ == "__main__":
    main()
