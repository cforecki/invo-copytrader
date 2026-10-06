#!/usr/bin/env python3
"""Walk-forward backtest of TA-Lib candlestick patterns on real Hyperliquid candles.

  python pattern_backtest.py                                   # BTC ETH SOL, 1h, 5000 bars
  python pattern_backtest.py --coins BTC ETH --interval 4h --horizons 3 6 12

Method (designed to NOT flatter the patterns):
  - Signal = a directional pattern on a CLOSED bar t (+ -> long, - -> short).
    Indecision/shape patterns (doji, spinning top, marubozu, ...) are excluded.
  - Entry at the OPEN of bar t+1 (no look-ahead); exit at the CLOSE of bar t+H.
  - Net return = direction * price move - 2 * fee (taker fee each side). 1x, no funding.
  - Each coin's history is split in time: first half = in-sample (IS), second
    half = out-of-sample (OOS). Patterns are SELECTED on IS only (n >= MIN_N and
    IS mean net return > 0), then judged on OOS data they never saw.
  - "Excess" = return minus the average same-direction return of entering on
    EVERY bar in that coin/half -- strips out plain trend drift (a "bullish"
    pattern looks great in a bull market for reasons unrelated to the pattern).
  - Holding windows overlap, so t-stats are optimistic; with ~50 patterns x
    several horizons, a few |t| > 2 results are expected by chance alone.

Output is evidence about the past few months of these coins only. Past results do
not guarantee future results.
"""

from __future__ import annotations

import argparse
import math
import statistics
import sys
from collections import defaultdict

import hl_client
import ta_features as T

FEE_PER_SIDE = 0.00045
MIN_N = 30


def trades_for_coin(coin, interval, bars, horizons, fee):
    df = T.ohlcv_frame(hl_client.candles(coin, interval, bars))
    pats = T.candle_patterns(df)
    o, c = df["open"].to_numpy(), df["close"].to_numpy()
    n = len(df)
    split = n // 2
    directional = [p for p in pats.columns if p not in T.NON_DIRECTIONAL]

    # baseline: mean forward return of entering long on every bar, per half and horizon
    base = {}
    for H in horizons:
        for half, rng in (("IS", range(0, split)), ("OOS", range(split, n))):
            rets = [c[t + H] / o[t + 1] - 1 for t in rng if t + H < n]
            base[(half, H)] = statistics.fmean(rets) if rets else 0.0

    out = []
    for p in directional:
        vals = pats[p].to_numpy()
        for t in range(n):
            v = vals[t]
            if not v:
                continue
            d = 1 if v > 0 else -1
            half = "IS" if t < split else "OOS"
            for H in horizons:
                if t + H >= n:
                    continue
                gross = d * (c[t + H] / o[t + 1] - 1)
                out.append({"coin": coin, "pattern": p, "H": H, "half": half, "t": t, "dir": d,
                            "net": gross - 2 * fee, "excess": gross - d * base[(half, H)]})
    span = (df.index[0], df.index[split], df.index[-1])
    bh = {"IS": c[split - 1] / o[0] - 1, "OOS": c[-1] / o[split] - 1}
    return out, span, bh, n


def stats(rs):
    if not rs:
        return None
    wins = [r for r in rs if r > 0]
    gp, gl = sum(wins), -sum(r for r in rs if r < 0)
    mu = statistics.fmean(rs)
    sd = statistics.stdev(rs) if len(rs) > 1 else 0.0
    return {"n": len(rs), "win": 100 * len(wins) / len(rs), "mean_bps": 1e4 * mu,
            "pf": gp / gl if gl > 0 else math.inf, "t": mu / (sd / math.sqrt(len(rs))) if sd > 0 else 0.0}


def sim_equity(trades, H):
    """Sequential, non-overlapping per coin: take a signal only when flat; 1x notional, full equity."""
    by_coin = defaultdict(list)
    for tr in trades:
        by_coin[tr["coin"]].append(tr)
    res = {}
    for coin, trs in by_coin.items():
        trs.sort(key=lambda x: x["t"])
        eq, peak, mdd, busy_until, n, wins = 1.0, 1.0, 0.0, -1, 0, 0
        for tr in trs:
            if tr["t"] <= busy_until:
                continue
            eq *= 1 + tr["net"]
            n += 1
            wins += tr["net"] > 0
            peak = max(peak, eq)
            mdd = max(mdd, 1 - eq / peak)
            busy_until = tr["t"] + H
        res[coin] = {"trades": n, "win": 100 * wins / n if n else 0, "ret": 100 * (eq - 1), "mdd": 100 * mdd}
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--coins", nargs="+", default=["BTC", "ETH", "SOL"])
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--bars", type=int, default=5000)
    ap.add_argument("--horizons", nargs="+", type=int, default=[4, 12, 24])
    ap.add_argument("--fee", type=float, default=FEE_PER_SIDE)
    a = ap.parse_args(argv)

    trades, bh = [], {}
    print(f"Pattern backtest | {a.interval} | fee {a.fee:.3%}/side | entry next open, exit close of bar t+H\n")
    for coin in a.coins:
        tr, span, b, n = trades_for_coin(coin, a.interval, a.bars, a.horizons, a.fee)
        trades += tr
        bh[coin] = b
        print(f"{coin}: {n} closed bars  IS {span[0]:%Y-%m-%d}..{span[1]:%Y-%m-%d} (buy&hold {100*b['IS']:+.1f}%)"
              f"  OOS {span[1]:%Y-%m-%d}..{span[2]:%Y-%m-%d} (buy&hold {100*b['OOS']:+.1f}%)")

    for H in a.horizons:
        print(f"\n=== Horizon {H} bars ===")
        allis = [t["net"] for t in trades if t["H"] == H and t["half"] == "IS"]
        alloos = [t["net"] for t in trades if t["H"] == H and t["half"] == "OOS"]
        for label, rs in (("ALL directional signals, IS ", allis), ("ALL directional signals, OOS", alloos)):
            s = stats(rs)
            print(f"{label}: n={s['n']:5d} win {s['win']:5.1f}%  mean {s['mean_bps']:+6.1f} bps net  PF {s['pf']:.2f}")

        groups = defaultdict(lambda: {"IS": [], "OOS": [], "exIS": [], "exOOS": []})
        for t in trades:
            if t["H"] == H:
                g = groups[t["pattern"]]
                g[t["half"]].append(t["net"])
                g["ex" + t["half"]].append(t["excess"])
        selected = []
        rows = []
        for p, g in groups.items():
            si, so = stats(g["IS"]), stats(g["OOS"])
            if not si or si["n"] < MIN_N:
                continue
            if si["mean_bps"] > 0:
                selected.append(p)
            rows.append((p, si, so, stats(g["exOOS"])))
        rows.sort(key=lambda r: -r[1]["mean_bps"])
        print(f"\n{'pattern':20} | {'IS n':>5} {'win%':>5} {'bps':>6} {'PF':>5} | "
              f"{'OOS n':>5} {'win%':>5} {'bps':>6} {'PF':>5} {'t':>5} | {'OOS excess bps':>14}")
        for p, si, so, sx in rows:
            mark = "*" if p in selected else " "
            if so:
                print(f"{mark}{p:19} | {si['n']:5d} {si['win']:5.1f} {si['mean_bps']:+6.1f} {si['pf']:5.2f} | "
                      f"{so['n']:5d} {so['win']:5.1f} {so['mean_bps']:+6.1f} {so['pf']:5.2f} {so['t']:+5.1f} | "
                      f"{sx['mean_bps']:+14.1f}")
            else:
                print(f"{mark}{p:19} | {si['n']:5d} {si['win']:5.1f} {si['mean_bps']:+6.1f} {si['pf']:5.2f} | (no OOS signals)")

        sel_oos = [t for t in trades if t["H"] == H and t["half"] == "OOS" and t["pattern"] in selected]
        survived = sum(1 for p, si, so, _ in rows if p in selected and so and so["mean_bps"] > 0)
        print(f"\nIS-selected patterns (*): {len(selected)}; still net-positive OOS: {survived}/{len(selected)}")
        s = stats([t["net"] for t in sel_oos])
        if s:
            print(f"OOS, all trades of IS-selected patterns: n={s['n']} win {s['win']:.1f}%  "
                  f"mean {s['mean_bps']:+.1f} bps net  PF {s['pf']:.2f}  t {s['t']:+.1f}")
            for coin, r in sim_equity(sel_oos, H).items():
                print(f"  OOS sequential sim {coin}: {r['trades']} trades, win {r['win']:.1f}%, "
                      f"return {r['ret']:+.1f}%, max DD {r['mdd']:.1f}%  (buy&hold {100*bh[coin]['OOS']:+.1f}%)")
    print("\nPast results on a few months of data do not guarantee future results. Analysis only.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
