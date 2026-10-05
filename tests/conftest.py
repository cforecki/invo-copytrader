import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def inv(i, coin="BTC", long=True, entry=100.0, exit_=None, lev=2, size=10.0,
        opened_days_ago=10.0, hold_days=1.0, sl=90.0, is_open=False, current=None):
    """Synthetic Invo investment dict, shaped like get_investments output. TEST FIXTURE ONLY."""
    opened = NOW - timedelta(days=opened_days_ago)
    d = {
        "baseId": f"t{i}", "ticker": coin, "directionLong": long, "leverage": lev, "entrySize": size,
        "entryPrice": entry, "createdAt": opened.isoformat(), "stopLoss": sl, "isOpen": is_open,
        "currentPrice": current if current is not None else entry,
    }
    if not is_open:
        d["closingPrice"] = exit_
        d["closedAt"] = (opened + timedelta(days=hold_days)).isoformat()
    return d


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    import hl_client

    def boom(*a, **k):
        raise hl_client.HyperliquidError("network disabled in tests")
    monkeypatch.setattr(hl_client, "all_mids", boom)
