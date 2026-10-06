#!/usr/bin/env python3
"""Optional technical analysis: TA-Lib candlestick patterns + indicators on real Hyperliquid candles.

  pip install -r requirements-analysis.txt
  python ta_features.py BTC --interval 1h --bars 500
  python ta_features.py ETH --interval 4h --all --csv eth_4h.csv

ANALYSIS / CONTEXT ONLY. Nothing here feeds the bot's trade decisions. Candlestick
patterns in particular have weak out-of-sample evidence, and none of these signals
has been validated against this strategy. Wiring any of them into entries/exits
would need its own backtest first. Past behaviour of a pattern does not guarantee
future results.

Pattern outputs follow TA-Lib's convention: +100 bullish, -100 bearish (some patterns,
e.g. CDLHIKKAKE, emit +/-200 for the confirmed variant), 0 = not present.
"""

from __future__ import annotations

import argparse
import sys

import hl_client

try:
    import pandas as pd
    import talib
    from talib import abstract
except ImportError as _e:  # pragma: no cover - exercised only without the extras
    _IMPORT_ERROR = _e
    talib = None
else:
    _IMPORT_ERROR = None


def _require():
    if talib is None:
        raise RuntimeError(
            f"TA-Lib/pandas not installed ({_IMPORT_ERROR}). Run: pip install -r requirements-analysis.txt"
        )


# TA-Lib emits a nonzero sign for these, but they are indecision / candle-shape descriptors,
# not directional calls: doji-family always returns +100; the others' sign is just candle colour.
NON_DIRECTIONAL = {
    "CDLDOJI": "indecision", "CDLLONGLEGGEDDOJI": "indecision", "CDLRICKSHAWMAN": "indecision",
    "CDLHIGHWAVE": "indecision", "CDLSPINNINGTOP": "indecision",
    "CDLLONGLINE": "candle shape", "CDLSHORTLINE": "candle shape",
    "CDLMARUBOZU": "candle shape", "CDLCLOSINGMARUBOZU": "candle shape",
}


def pattern_label(name: str, value: int) -> str:
    if name in NON_DIRECTIONAL:
        if name in ("CDLDOJI", "CDLLONGLEGGEDDOJI", "CDLRICKSHAWMAN"):
            return NON_DIRECTIONAL[name]  # always +100; sign carries no information
        return NON_DIRECTIONAL[name] + (" (white)" if value > 0 else " (black)")
    return "bullish" if value > 0 else "bearish"


DEFAULT_INDICATORS = ["RSI14", "MACD", "BBANDS20", "ATR14", "ADX14", "EMA20", "EMA50", "EMA200", "OBV"]
# Groups --all runs besides patterns. Math Operators/Transform are elementwise arithmetic, not indicators.
ALL_GROUPS = ["Overlap Studies", "Momentum Indicators", "Volume Indicators", "Volatility Indicators",
              "Cycle Indicators", "Price Transform", "Statistic Functions"]


def ohlcv_frame(raw: list[dict], drop_forming: bool = True, now_ms: int | None = None) -> "pd.DataFrame":
    """HL candleSnapshot dicts -> float64 OHLCV DataFrame indexed by UTC bar-open time."""
    _require()
    if not raw:
        raise ValueError("no candles")
    df = pd.DataFrame(raw)
    df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype("float64")
    df.index = pd.to_datetime(df["t"], unit="ms", utc=True)
    df = df.sort_index()
    df = df[~df.index.duplicated(keep="last")]
    if drop_forming:
        import time
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        df = df[df["T"] < now_ms]  # keep only bars whose close time has passed
    return df[["open", "high", "low", "close", "volume"]]


def _arrays(df):
    return {k: df[k].to_numpy(dtype="float64") for k in ("open", "high", "low", "close", "volume")}


def candle_patterns(df: "pd.DataFrame") -> "pd.DataFrame":
    """All 61 TA-Lib CDL* patterns as int columns (+100/-100/+-200/0)."""
    _require()
    a = _arrays(df)
    out = {}
    for name in talib.get_function_groups()["Pattern Recognition"]:
        out[name] = getattr(talib, name)(a["open"], a["high"], a["low"], a["close"])
    return pd.DataFrame(out, index=df.index).astype("int32")


def pattern_hits(patterns: "pd.DataFrame", last_n: int = 5) -> list[tuple]:
    """(bar time, pattern, value) for every nonzero pattern in the last N bars, newest first."""
    tail = patterns.tail(last_n)
    hits = []
    for ts, row in tail.iloc[::-1].iterrows():
        for name, v in row.items():
            if v:
                hits.append((ts, name, int(v)))
    return hits


def indicators(df: "pd.DataFrame", names: list[str] | None = None) -> "pd.DataFrame":
    """Default indicator set. Column names say the parameters used."""
    _require()
    a = _arrays(df)
    h, low, c, v = a["high"], a["low"], a["close"], a["volume"]
    out = {}
    for n in names or DEFAULT_INDICATORS:
        if n == "RSI14":
            out["RSI14"] = talib.RSI(c, 14)
        elif n == "MACD":
            out["MACD"], out["MACD_signal"], out["MACD_hist"] = talib.MACD(c, 12, 26, 9)
        elif n == "BBANDS20":
            out["BB_upper"], out["BB_mid"], out["BB_lower"] = talib.BBANDS(c, 20, 2.0, 2.0)
        elif n == "ATR14":
            out["ATR14"] = talib.ATR(h, low, c, 14)
        elif n == "ADX14":
            out["ADX14"] = talib.ADX(h, low, c, 14)
        elif n.startswith("EMA"):
            out[n] = talib.EMA(c, int(n[3:]))
        elif n == "OBV":
            out["OBV"] = talib.OBV(c, v)
        else:
            raise ValueError(f"unknown indicator {n!r}; known: {DEFAULT_INDICATORS}")
    return pd.DataFrame(out, index=df.index)


def all_indicators(df: "pd.DataFrame") -> tuple["pd.DataFrame", list[str]]:
    """Every TA-Lib function in ALL_GROUPS with default parameters. Returns (frame, skipped)."""
    _require()
    inputs = _arrays(df)
    groups = talib.get_function_groups()
    out, skipped = {}, []
    for g in ALL_GROUPS:
        for name in groups[g]:
            try:
                fn = abstract.Function(name)
                res = fn(inputs)
            except Exception as e:  # e.g. MAVP needs a 'periods' series we don't have
                skipped.append(f"{name}: {e}")
                continue
            outs = fn.output_names
            if len(outs) == 1:
                out[name] = res
            else:
                for oname, arr in zip(outs, res):
                    out[f"{name}_{oname}"] = arr
    return pd.DataFrame(out, index=df.index), skipped


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("coin")
    ap.add_argument("--interval", default="1h", choices=sorted(hl_client.INTERVAL_MS))
    ap.add_argument("--bars", type=int, default=500)
    ap.add_argument("--last", type=int, default=5, help="report patterns in the last N closed bars")
    ap.add_argument("--all", action="store_true", help="every TA-Lib indicator, not just the default set")
    ap.add_argument("--include-forming", action="store_true", help="keep the still-open last bar")
    ap.add_argument("--csv", help="write OHLCV + indicators + patterns to this CSV")
    a = ap.parse_args(argv)
    try:
        _require()
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 2

    df = ohlcv_frame(hl_client.candles(a.coin, a.interval, a.bars), drop_forming=not a.include_forming)
    if a.all:
        ind, skipped = all_indicators(df)
    else:
        ind, skipped = indicators(df), []
    pats = candle_patterns(df)

    last = df.index[-1]
    print(f"{hl_client.normalize_coin(a.coin)} {a.interval}: {len(df)} closed bars, last bar opened {last}")
    print(f"close {df['close'].iloc[-1]:.6g}")
    print("\nIndicators (last closed bar):")
    for col, val in ind.iloc[-1].items():
        print(f"  {col:24} {'NaN (warm-up)' if pd.isna(val) else f'{val:.6g}'}")
    if skipped:
        print(f"\nSkipped {len(skipped)} functions needing extra inputs: " + ", ".join(s.split(':')[0] for s in skipped))
    hits = pattern_hits(pats, a.last)
    print(f"\nCandlestick patterns in last {a.last} closed bars:")
    for ts, name, v in hits:
        print(f"  {ts}  {name:22} {pattern_label(name, v):20} ({v:+d})")
    if not hits:
        print("  none")
    print("\nAnalysis only -- not a trading signal; not used by bot.py.")
    if a.csv:
        pd.concat([df, ind, pats], axis=1).to_csv(a.csv)
        print(f"wrote {a.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
