"""Hyperliquid public /info API (no auth) -- prices and on-chain verification.

Used for:
  - mid prices for paper fills and mark-to-market (real exchange prices,
    never synthesized);
  - optional verification of a trader's Invo track record against their
    actual Hyperliquid wallet, when you know the address (Invo does not
    reliably expose it, so it's supplied by you in the portfolio list).
"""

from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger("invo.hl")

INFO_URL = "https://api.hyperliquid.xyz/info"
TIMEOUT_S = 15


class HyperliquidError(RuntimeError):
    pass


def _info(body: dict, session: requests.Session | None = None):
    http = session or requests
    try:
        resp = http.post(INFO_URL, json=body, timeout=TIMEOUT_S)
    except requests.RequestException as e:
        raise HyperliquidError(f"HL /info {body.get('type')}: {e}") from e
    if resp.status_code != 200:
        raise HyperliquidError(f"HL /info {body.get('type')} HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def normalize_coin(ticker: str) -> str:
    """Invo tickers look like 'BTC', 'btc', 'BTC-PERP', 'BTC/USD'. HL /info wants bare 'BTC'."""
    t = (ticker or "").upper().strip()
    for suffix in ("-PERP", "/USDT", "/USDC", "/USD", "USDT", "-USD"):
        if t.endswith(suffix) and len(t) > len(suffix):
            t = t[: -len(suffix)]
            break
    return t


def all_mids(session=None) -> dict[str, float]:
    raw = _info({"type": "allMids"}, session)
    out = {}
    for k, v in raw.items():
        if k.startswith("@") or k.startswith("#"):
            continue  # spot-pair / outcome indices, not perp coins
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            pass
    return out


def clearinghouse_state(address: str, session=None) -> dict:
    return _info({"type": "clearinghouseState", "user": address}, session)


def user_fills_since(address: str, start_ms: int, session=None, max_pages: int = 20) -> list[dict]:
    """All fills since start_ms (HL returns <=2000 per call; we page by time)."""
    fills: list[dict] = []
    cursor = start_ms
    for _ in range(max_pages):
        batch = _info({"type": "userFillsByTime", "user": address, "startTime": cursor, "aggregateByTime": True}, session)
        if not batch:
            break
        fills.extend(batch)
        last = max(f["time"] for f in batch)
        if len(batch) < 2000 or last <= cursor:
            break
        cursor = last + 1
    return fills


def verify_wallet(address: str, days: int = 90) -> dict:
    """Summarize realized PnL from on-chain fills for cross-checking Invo stats.

    Only fills with a nonzero closedPnl count as closing trades. Fees are
    included as a separate total. This is a *sanity check* on the Invo
    record (does the wallet actually trade, and is it net positive), not a
    replacement for it.
    """
    start_ms = int((time.time() - days * 86400) * 1000)
    fills = user_fills_since(address, start_ms)
    closing = [f for f in fills if float(f.get("closedPnl", 0) or 0) != 0]
    pnl = sum(float(f["closedPnl"]) for f in closing)
    fees = sum(float(f.get("fee", 0) or 0) for f in fills)
    wins = sum(1 for f in closing if float(f["closedPnl"]) > 0)
    state = clearinghouse_state(address)
    acct_value = float(state.get("marginSummary", {}).get("accountValue", 0) or 0)
    return {
        "hl_window_days": days,
        "hl_fills": len(fills),
        "hl_closing_fills": len(closing),
        "hl_realized_pnl_usd": round(pnl, 2),
        "hl_fees_usd": round(fees, 2),
        "hl_net_pnl_usd": round(pnl - fees, 2),
        "hl_win_rate_pct": round(100 * wins / len(closing), 1) if closing else None,
        "hl_account_value_usd": round(acct_value, 2),
    }
