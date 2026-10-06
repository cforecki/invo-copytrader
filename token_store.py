"""Persistent, owner-only storage for Invo tokens.

Refreshed access tokens (and a rotated refresh token, if Invo ever issues one) are
saved here after every successful refresh, so a restart resumes with the newest
tokens instead of falling back to a stale env var.

  - Path: INVO_TOKEN_FILE, default ~/.config/invo-copytrader/tokens.json (outside the repo).
  - File mode 0600, directory 0700. Looser permissions are tightened on load with a warning.
  - Atomic writes (temp file + fsync + rename): a crash never leaves a half-written file.
  - Token VALUES are never logged or printed by this module.
  - INVO_PERSIST_TOKENS=0 disables persistence entirely.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import time
from pathlib import Path

log = logging.getLogger("invo.tokens")


def default_path() -> Path:
    return Path(os.environ.get("INVO_TOKEN_FILE") or Path.home() / ".config" / "invo-copytrader" / "tokens.json")


def persistence_enabled() -> bool:
    return os.environ.get("INVO_PERSIST_TOKENS", "1").strip().lower() not in ("0", "false", "no", "off")


class TokenStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else default_path()

    def load(self) -> dict:
        if not self.path.exists():
            return {}
        mode = stat.S_IMODE(self.path.stat().st_mode)
        if mode & 0o077:
            log.warning("Token file %s had permissions %o; tightening to 600", self.path, mode)
            os.chmod(self.path, 0o600)
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError) as e:
            log.error("Token file %s unreadable (%s); ignoring it", self.path, type(e).__name__)
            return {}
        return data if isinstance(data, dict) else {}

    def save(self, refresh_token: str | None, access_token: str | None,
             refresh_expires: float | None, access_expires: float | None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        payload = {
            "refresh_token": refresh_token,
            "access_token": access_token,
            "refresh_expires": refresh_expires,
            "access_expires": access_expires,
            "updated_at": time.time(),
        }
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh)
                fh.flush()
                os.fsync(fh.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
