import pytest

pytest.importorskip("talib")
import pattern_backtest as PB  # noqa: E402


def test_stats_hand_computed():
    s = PB.stats([0.02, -0.01, 0.03, -0.02])
    assert s["n"] == 4 and s["win"] == 50
    assert s["mean_bps"] == pytest.approx(50.0)
    assert s["pf"] == pytest.approx(0.05 / 0.03)


def test_sim_equity_is_non_overlapping_and_compounds():
    trs = [{"coin": "X", "t": 0, "net": 0.10}, {"coin": "X", "t": 2, "net": -0.50},  # t=2 overlaps H=4
           {"coin": "X", "t": 5, "net": -0.10}]
    r = PB.sim_equity(trs, H=4)["X"]
    assert r["trades"] == 2
    assert r["ret"] == pytest.approx(100 * (1.1 * 0.9 - 1))
    assert r["mdd"] == pytest.approx(10.0)


def test_no_lookahead_entry_is_next_open(monkeypatch):
    import hl_client
    from test_ta_features import downtrend_then_engulf, raw
    bars = downtrend_then_engulf()
    px = bars[-1][3]
    bars += [(px + i, px + i + 1, px + i - 1, px + i + 0.5, 100) for i in range(1, 8)]  # bars after signal
    candles = raw(bars)
    monkeypatch.setattr(hl_client, "candles", lambda *a, **k: candles)
    trades, *_ = PB.trades_for_coin("X", "1h", len(bars), [1, 3], 0.0)
    o = [b[0] for b in bars]
    c = [b[3] for b in bars]
    eng = [t for t in trades if t["pattern"] == "CDLENGULFING" and t["dir"] == 1]
    assert eng, "fixture must produce the bullish engulfing trade"
    assert trades
    for tr in trades:
        t, d, h = tr["t"], tr["dir"], tr["H"]
        assert tr["net"] == pytest.approx(d * (c[t + h] / o[t + 1] - 1))  # entry = NEXT bar open
