"""Bounded, screenshot-free diagnostics for intermittent tracking failures."""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path


class DiagnosticJournal:
    def __init__(self, directory: Path, max_bytes: int = 500_000):
        self.path = directory / "tracker_diagnostics.log"
        self.previous_path = directory / "tracker_diagnostics.previous.log"
        self.max_bytes = max_bytes
        self.lock = threading.Lock()

    def write(self, kind: str, **values) -> None:
        """Keep only state transitions, never capture screenshots or keystrokes."""
        try:
            event = {"at": datetime.now().astimezone().isoformat(timespec="seconds"),
                     "event": kind, **values}
            line = json.dumps(event, ensure_ascii=False, default=str) + "\n"
            with self.lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if (self.path.exists() and
                        self.path.stat().st_size + len(line.encode("utf-8")) > self.max_bytes):
                    self.path.replace(self.previous_path)
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(line)
        except OSError:
            # Diagnostics should never interrupt actual gameplay or the UI.
            pass
