"""trend_research tests on hand-built series (TEST FIXTURES, not market data)."""

import json
import math

import numpy as np
import pytest

pytest.importorskip("talib")
import strategy_backtest as SB  # noqa: E402
import trend_research as TR  # noqa: E402

DAY = 86_400_000


def test_vol_weight_uses_only_past_bars():
    rng = np.random.default_rng(1)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, 80)))
    t = 50
    w = TR.vol_weight(c, t, 365)
    assert 0 < w <= 1
    c2 = c.copy()
    c2[t + 1:] *= np.exp(np.cumsum(rng.normal(0, 0.5, len(c) - t - 1)))  # wild FUTURE bars
    assert TR.vol_weight(c2, t, 365) == pytest.approx(w)                  # unchanged: no look-ahead
    c3 = c.copy()
    c3[t] *= 1.3                                                          # bar t itself IS used
    assert TR.vol_weight(c3, t, 365) != pytest.approx(w)


def test_vol_weight_cap_and_inverse_vol():
    calm = 100 * np.exp(np.cumsum(np.tile([0.001, -0.001], 40)))
    wild = 100 * np.exp(np.cumsum(np.tile([0.05, -0.05], 40)))
    w_calm, w_wild = TR.vol_weight(calm, 60, 365), TR.vol_weight(wild, 60, 365)
    assert w_calm == 1.0                       # capped
    assert w_wild < w_calm
    ann = 0.05 * math.sqrt(365) * math.sqrt(31 / 30)  # sample stdev of alternating +-0.05 over 31 obs
    assert w_wild == pytest.approx(TR.TARGET_VOL / ann, rel=0.05)
    assert TR.vol_weight(calm, 5, 365) == 0.0  # not enough history


def test_funding_between_and_sign():
    f = TR.Funding([(h * 3_600_000, 0.0001) for h in range(48)])
    assert f.between(0, 24 * 3_600_000) == pytest.approx(24 * 0.0001)
    assert f.between(10, 5) == 0.0
    assert f.start_ms == 0


def test_fetch_funding_pages_and_caches(tmp_path):
    calls = []

    dataset = [{"time": t, "fundingRate": "0.0001"} for t in range(1003)]  # what the server holds

    def fake_info(body):
        calls.append(body["startTime"])
        return [r for r in dataset if r["time"] >= body["startTime"]][:500]

    rows = TR.fetch_funding("X", tmp_path, info=fake_info, sleep_s=0, now_ms=10_000)
    assert len(rows) == 1003 and calls == [0, 500, 1000]
    calls.clear()
    rows2 = TR.fetch_funding("X", tmp_path, info=fake_info, sleep_s=0, now_ms=10_000)
    assert len(rows2) == 1003 and calls == [1003]            # resumes from cache
    assert json.loads((tmp_path / "X.json").read_text())[0] == [0, 0.0001]


def make_trend(n=120):
    c = np.concatenate([np.full(40, 100.0) * np.exp(np.linspace(0, 0.01, 40)),
                        100 * np.exp(np.linspace(0.01, 0.5, n - 40))])
    o = np.r_[c[0], c[:-1]]
    return o, np.maximum(o, c) * 1.001, np.minimum(o, c) * 0.999, c


def test_sleeve_equity_matches_hand_computation_with_fees_and_funding():
    o, h, lo, c = make_trend()
    n = len(c)
    times = np.arange(n, dtype="int64") * DAY
    entry = np.zeros(n, dtype=int)
    entry[60] = 1
    exit_ = np.zeros(n, dtype=bool)
    exit_[90] = True
    sig = SB.Signals(entry, exit_, np.zeros(n, dtype=bool))
    fund = TR.Funding([(h * 3_600_000, 0.00001) for h in range(n * 24)])
    fee = 0.001
    s = TR.sleeve_equity(o, h, lo, c, times, DAY, sig, 365, fee, fund)
    assert len(s.trades) == 1
    tr = s.trades[0]
    assert (tr["entry_t"], tr["exit_t"]) == (61, 91)
    w = TR.vol_weight(c, 60, 365)
    assert tr["weight"] == pytest.approx(w) and w > 0
    f_paid = 30 * 24 * 0.00001                       # open of bar 61 -> open of bar 91
    expected = 1 + w * ((o[91] / o[61] - 1) - 2 * fee - f_paid)
    assert s.equity[-1] == pytest.approx(expected)
    # mark-to-market mid-trade: entry fee + accrued funding to that close
    t = 75
    f_t = (t - 61 + 1) * 24 * 0.00001
    assert s.equity[t] == pytest.approx(1 + w * ((c[t] / o[61] - 1) - fee - f_t))
    assert s.equity[:61].tolist() == [1.0] * 61
    assert s.in_market[61:91].all() and not s.in_market[:61].any()


def test_short_receives_positive_funding():
    o, h, lo, c = make_trend()
    n = len(c)
    times = np.arange(n, dtype="int64") * DAY
    entry = np.zeros(n, dtype=int)
    entry[60] = -1
    sig = SB.Signals(entry, np.zeros(n, dtype=bool), np.r_[np.zeros(70, dtype=bool), True,
                                                             np.zeros(n - 71, dtype=bool)])
    with_f = TR.sleeve_equity(o, h, lo, c, times, DAY, sig, 365, 0.0,
                              TR.Funding([(k * 3_600_000, 0.001) for k in range(n * 24)]))
    no_f = TR.sleeve_equity(o, h, lo, c, times, DAY, sig, 365, 0.0, None)
    assert with_f.trades and with_f.equity[-1] > no_f.equity[-1]


def test_portfolio_keeps_late_sleeve_in_cash():
    a = TR.Sleeve("A", np.array([0, 1, 2, 3]) * DAY, np.array([1.0, 1.1, 1.2, 1.3]), [], np.ones(4, bool))
    b = TR.Sleeve("B", np.array([2, 3]) * DAY, np.array([1.0, 0.5]), [], np.ones(2, bool))
    grid, eq = TR.portfolio({"A": a, "B": b})
    assert grid.tolist() == [0, DAY, 2 * DAY, 3 * DAY]
    assert eq.tolist() == pytest.approx([1.0, 1.05, 1.1, 0.9])


def test_metrics_drawdown_and_total():
    m = TR.metrics(np.array([1.0, 1.2, 0.9, 1.1]), 365)
    assert m["total"] == pytest.approx(10.0)
    assert m["mdd"] == pytest.approx(25.0)


def test_bar_times_are_epoch_ms_regardless_of_pandas_unit():
    import pandas as pd
    for unit in ("ns", "us", "ms"):
        idx = pd.DatetimeIndex(pd.to_datetime([1_700_000_000_000, 1_700_086_400_000], unit="ms", utc=True)).as_unit(unit)
        df = pd.DataFrame({"close": [1.0, 2.0]}, index=idx)
        assert TR.bar_times_ms(df).tolist() == [1_700_000_000_000, 1_700_086_400_000], unit
    bad = pd.DataFrame({"close": [1.0]}, index=pd.to_datetime([5], unit="ms", utc=True))
    with pytest.raises(ValueError, match="implausible"):
        TR.bar_times_ms(bad)


def test_load_market_times_match_candle_open_times(monkeypatch):
    import hl_client
    t0 = 1_700_000_000_000
    raw = [{"t": t0 + i * DAY, "T": t0 + (i + 1) * DAY - 1, "o": "1", "h": "1", "l": "1", "c": "1", "v": "1", "n": 1}
           for i in range(5)]
    monkeypatch.setattr(hl_client, "candles", lambda *a, **k: raw)
    data = TR.load_market(["X"], "1d", 5)
    assert data["X"][4].tolist() == [r["t"] for r in raw]
