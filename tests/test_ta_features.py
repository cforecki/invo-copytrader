"""TA-Lib wrapper tests on hand-built bars (TEST FIXTURES, not market data)."""

import math

import pytest

talib = pytest.importorskip("talib")
import ta_features as T  # noqa: E402

H = 3_600_000


def raw(bars, t0=1_700_000_000_000):
    """(o, h, l, c, v) tuples -> HL candleSnapshot-shaped dicts (strings, like the real API)."""
    return [{"t": t0 + i * H, "T": t0 + (i + 1) * H - 1, "s": "TEST", "i": "1h",
             "o": str(o), "h": str(h), "l": str(lo), "c": str(c), "v": str(v), "n": 1}
            for i, (o, h, lo, c, v) in enumerate(bars)]


def downtrend_then_engulf():
    bars = []
    px = 120.0
    for _ in range(12):  # steady downtrend of black candles
        bars.append((px, px + 0.5, px - 2.5, px - 2.0, 100))
        px -= 2.0
    # small black candle, then a large white candle whose body engulfs it
    bars.append((px, px + 0.3, px - 1.3, px - 1.0, 100))
    bars.append((px - 1.5, px + 2.5, px - 1.8, px + 2.0, 300))
    return bars


def test_ohlcv_frame_parses_and_drops_forming_bar():
    r = raw([(1, 2, 0.5, 1.5, 10), (1.5, 2, 1, 1.8, 11), (1.8, 2, 1.7, 1.9, 12)])
    now = r[-1]["T"] - 1  # last bar not closed yet
    df = T.ohlcv_frame(r, now_ms=now)
    assert len(df) == 2 and df["close"].dtype == "float64"
    assert str(df.index.tz) == "UTC"
    assert len(T.ohlcv_frame(r, drop_forming=False)) == 3


def test_bullish_engulfing_detected_at_known_bar():
    df = T.ohlcv_frame(raw(downtrend_then_engulf()), drop_forming=False)
    pats = T.candle_patterns(df)
    assert pats.shape[1] == 61
    assert pats["CDLENGULFING"].iloc[-1] == 100
    assert (pats["CDLENGULFING"].iloc[:-1] != 100).all()  # bullish engulfing only on the last bar
    hits = T.pattern_hits(pats, last_n=1)
    assert any(name == "CDLENGULFING" and v == 100 for _, name, v in hits)


def test_default_indicators_columns_and_warmup():
    bars = [(100 + math.sin(i / 3) * 5, 106 + math.sin(i / 3) * 5, 94 + math.sin(i / 3) * 5,
             100 + math.sin((i + 1) / 3) * 5, 100 + i) for i in range(260)]
    df = T.ohlcv_frame(raw(bars), drop_forming=False)
    ind = T.indicators(df)
    for col in ("RSI14", "MACD", "MACD_signal", "MACD_hist", "BB_upper", "BB_mid", "BB_lower",
                "ATR14", "ADX14", "EMA20", "EMA50", "EMA200", "OBV"):
        assert col in ind.columns
    assert ind["RSI14"].iloc[:14].isna().all() and not ind["RSI14"].iloc[14:].isna().any()
    assert ind["EMA200"].iloc[:199].isna().all() and not math.isnan(ind["EMA200"].iloc[-1])
    assert ((ind["RSI14"].dropna() >= 0) & (ind["RSI14"].dropna() <= 100)).all()
    assert (ind["BB_upper"].dropna() >= ind["BB_lower"].dropna()).all()


def test_all_indicators_runs_every_group():
    bars = [(100 + i % 7, 103 + i % 7, 98 + i % 7, 101 + i % 5, 50 + i) for i in range(300)]
    ind, skipped = T.all_indicators(T.ohlcv_frame(raw(bars), drop_forming=False))
    assert ind.shape[1] > 100
    assert [s.split(":")[0] for s in skipped] == ["MAVP"]


def test_non_directional_labels():
    assert T.pattern_label("CDLDOJI", 100) == "indecision"
    assert T.pattern_label("CDLSPINNINGTOP", -100) == "indecision (black)"
    assert T.pattern_label("CDLENGULFING", -100) == "bearish"
    assert T.pattern_label("CDLHAMMER", 100) == "bullish"


def test_unknown_indicator_rejected():
    df = T.ohlcv_frame(raw([(1, 2, 0.5, 1.5, 10)] * 3), drop_forming=False)
    with pytest.raises(ValueError):
        T.indicators(df, ["NOPE"])
