#!/usr/bin/env python3
"""Manage the saved Invo tokens. Token values are never printed.

  python invo_auth.py import     # paste a refresh token once (hidden input); validated, then saved
  python invo_auth.py status     # expiry dates + days left, where the token came from
  python invo_auth.py refresh    # force a refresh now and save the result

After `import` (or with INVO_REFRESH_TOKEN set), screener.py and bot.py refresh the
short-lived access token automatically and save every refresh to the token file
(INVO_TOKEN_FILE, default ~/.config/invo-copytrader/tokens.json, mode 600).

The FIRST token cannot be generated automatically: Invo logs in through its web app
(Turnkey passkey / email code) and exposes no login API. Capture it once from the
browser -- see README "Getting your Invo JWT". A refresh token lasts about a year.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import time

from invo_client import InvoAuthError, InvoClient, InvoError, _strip_bearer, jwt_expiry
from token_store import TokenStore, persistence_enabled

WARN_DAYS = 14


def _fmt(exp):
    if exp is None:
        return "unknown (not decodable)"
    days = (exp - time.time()) / 86400
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(exp))
    return f"{when}  ({'EXPIRED' if days < 0 else f'{days:.1f} days left'})"


def cmd_status(_a) -> int:
    store = TokenStore()
    print(f"token file: {store.path} ({'persistence on' if persistence_enabled() else 'persistence OFF'})")
    saved = store.load() if persistence_enabled() else {}
    if saved:
        print(f"  saved refresh token expires: {_fmt(saved.get('refresh_expires'))}")
        print(f"  saved access token expires:  {_fmt(saved.get('access_expires'))}")
        print(f"  last saved: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(saved.get('updated_at', 0)))}")
    else:
        print("  (no saved tokens)")
    env = _strip_bearer(os.environ.get("INVO_REFRESH_TOKEN"))
    print(f"env INVO_REFRESH_TOKEN: {'set, expires ' + _fmt(jwt_expiry(env)) if env else 'not set'}")
    if env and saved.get("refresh_token") and env != saved.get("refresh_token"):
        newer = (jwt_expiry(saved["refresh_token"]) or 0) > (jwt_expiry(env) or 0)
        print("  NOTE: env and file hold different refresh tokens; "
              + ("the FILE one is newer -- update the env var / cloud environment setting to it."
                 if newer else "the env one is newer and will be used (and saved)."))
    try:
        c = InvoClient()
    except InvoAuthError as e:
        print(f"\nusable credentials: NO -- {e.reason}")
        return 2
    days = c.refresh_token_days_remaining()
    print(f"\nwill use refresh token from: {c.refresh_source}")
    if days is not None and days < WARN_DAYS:
        print(f"WARNING: refresh token expires in {days:.1f} days -- capture a new one soon (README).")
    return 0


def cmd_import(a) -> int:
    if a.stdin:
        tok = sys.stdin.readline().strip()
    else:
        tok = getpass.getpass("Paste Invo refresh token (input hidden): ").strip()
    tok = _strip_bearer(tok)
    if not tok or tok.count(".") != 2:
        print("That doesn't look like a JWT (expected three dot-separated parts).", file=sys.stderr)
        return 2
    exp = jwt_expiry(tok)
    if exp is not None and exp < time.time():
        print(f"That token already expired ({_fmt(exp)}). Capture a fresh one.", file=sys.stderr)
        return 2
    # Validate against Invo BEFORE saving: a bad token must never overwrite a good saved one.
    try:
        c = InvoClient(refresh_token=tok, persist=False)
        c.refresh()
    except InvoAuthError as e:
        print(f"Invo rejected the token; nothing was saved.\n\n{e}", file=sys.stderr)
        return 2
    except InvoError as e:
        print(f"Could not reach Invo to validate ({e}); nothing was saved.", file=sys.stderr)
        return 1
    if not persistence_enabled():
        print("Token is valid, but INVO_PERSIST_TOKENS is off -- not saved.")
        return 0
    store = TokenStore()
    store.save(c.refresh_token, c.access_token, jwt_expiry(c.refresh_token), jwt_expiry(c.access_token))
    print(f"Saved to {store.path} (mode 600). Refresh token expires {_fmt(jwt_expiry(c.refresh_token))}.")
    return 0


def cmd_refresh(_a) -> int:
    try:
        c = InvoClient()
        c.refresh()
    except InvoAuthError as e:
        print(str(e), file=sys.stderr)
        return 2
    except InvoError as e:
        print(f"Refresh failed: {e}", file=sys.stderr)
        return 1
    print(f"Refreshed. Access token expires {_fmt(jwt_expiry(c.access_token))}; "
          f"{'saved to ' + str(c.store.path) if c.store else 'persistence off, not saved'}.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    imp = sub.add_parser("import", help="save a refresh token (validated first)")
    imp.add_argument("--stdin", action="store_true", help="read the token from stdin instead of a hidden prompt")
    sub.add_parser("status", help="show expiry info (never token values)")
    sub.add_parser("refresh", help="force a refresh and save")
    a = ap.parse_args(argv)
    return {"import": cmd_import, "status": cmd_status, "refresh": cmd_refresh}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
