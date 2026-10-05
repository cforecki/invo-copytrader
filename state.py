"""Crash-safe JSON state + append-only fills log.

Writes go to a temp file, are fsync'd, then atomically renamed over the
old file, so a crash mid-write leaves the previous state intact. State is
saved after every fill, not only at the end of a cycle.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

SCHEMA_VERSION = 1


def new_state(mode: str, venue: str) -> dict:
    return {
        "schema": SCHEMA_VERSION,
        "mode": mode,
        "venue": venue,
        "created_at": time.time(),
        "starting_equity": None,       # set on first cycle
        "peak_equity": None,
        "last_equity": None,
        "realized_pnl": 0.0,
        "halted": False,
        "halt_reason": "",
        "positions": {},               # trader baseId -> our position
        "portfolios": {},              # portfolio id -> {initialized, last_open_ids, preexisting, missing_counts}
        "n_fills": 0,
        "last_cycle_at": None,
    }


class StateStore:
    def __init__(self, path: str, fills_log: str):
        self.path = Path(path)
        self.fills_path = Path(fills_log)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fills_path.parent.mkdir(parents=True, exist_ok=True)

    def load(self, mode: str, venue: str) -> dict:
        if not self.path.exists():
            return new_state(mode, venue)
        st = json.loads(self.path.read_text())
        if st.get("mode") != mode or st.get("venue") != venue:
            raise RuntimeError(
                f"{self.path} was written by mode={st.get('mode')} venue={st.get('venue')}, but this run is "
                f"mode={mode} venue={venue}. Use a different STATE_FILE (paper and live state must never mix)."
            )
        return st

    def save(self, st: dict) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w") as fh:
            json.dump(st, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    def log_fill(self, record: dict) -> None:
        with open(self.fills_path, "a") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
