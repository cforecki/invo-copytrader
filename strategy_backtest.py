#!/usr/bin/env python3
"""Backtest classic indicator strategies on real Hyperliquid candles, ranked by win rate.

  python strategy_backtest.py                      # BTC ETH SOL, 1h, 5000 bars
  python strategy_backtest.py --interval 4h

WIN RATE ALONE IS A TRAP. The designs with the highest win rates (mean reversion,
tight take-profit + wide stop, grid, martingale) win often by banking small gains
and occasionally taking a large loss. The table therefore shows expectancy, profit
factor, drawdown and out-of-sample results next to win rate, and flags
HIGH-WIN/NEG-EXP rows.

Simulation rules (chosen to avoid flattering results):
  - Signals computed on the CLOSE of bar t; orders fill at the OPEN of bar t+1.
  - TP/SL are checked intrabar against high/low; if one bar touches both, the
    STOP is assumed to hit first. A gap through the stop fills at the (worse) open.
  - One position per coin, 1x notional, full equity, compounding.
  - Taker fee both sides (default 0.045%). Funding is NOT modelled.
  - Textbook parameters, fixed -- nothing is optimized, so nothing is curve-fit.
  - History split in time: first half in-sample (IS), second half out-of-sample (OOS).
    With fixed parameters IS/OOS is a stability check across two regimes.
  - Grid / martingale are deliberately NOT simulated: their defining risk is a rare
    blow-up that a few months of data will usually not contain, so a short backtest
    would show a misleadingly perfect record.

Evidence about a few months of three coins only. Past results do not guarantee
future results.
"""

from __future__ import annotations

import argparse
import math
import statistics
import sys
from dataclasses import dataclass

import numpy as np
import talib

import hl_client
import ta_features as T

FEE_PER_SIDE = 0.00045
MIN_N = 30


@dataclass
class Signals:
    entry: np.ndarray        # +1 long / -1 short / 0, decided on the close of each bar
    exit_long: np.ndarray    # bool, decided on close
    exit_short: np.ndarray   # bool, decided on close
    tp: float | None = None  # fraction, e.g. 0.005
    sl: float | None = None
    max_hold: int | None = None
    flip: bool = False       # always-in: an opposite entry closes and reverses


def _cross_up(a, b):
    out = np.zeros(len(a), dtype=bool)
    out[1:] = (a[1:] > b[1:]) & (a[:-1] <= b[:-1])
    return out


def _shift(x, k=1):
    out = np.full(len(x), np.nan)
    out[k:] = x[:-k]
    return out


# ----------------------------------------------------------------- strategies
def s_rsi14_reversion(o, h, l, c):
    r = talib.RSI(c, 14)
    long_ = _cross_up(r, np.full(len(r), 30.0))
    short = _cross_up(np.full(len(r), 70.0), r)
    return Signals(np.where(long_, 1, np.where(short, -1, 0)), r >= 50, r <= 50, max_hold=48)


def s_connors_rsi2(o, h, l, c):
    r2, ema200, sma5 = talib.RSI(c, 2), talib.EMA(c, 200), talib.SMA(c, 5)
    long_ = (r2 < 10) & (c > ema200)
    short = (r2 > 90) & (c < ema200)
    return Signals(np.where(long_, 1, np.where(short, -1, 0)), c > sma5, c < sma5, max_hold=48)


def s_bollinger_reversion(o, h, l, c):
    up, mid, lo = talib.BBANDS(c, 20, 2.0, 2.0)
    return Signals(np.where(c < lo, 1, np.where(c > up, -1, 0)), c >= mid, c <= mid, max_hold=48)


def s_macd_cross(o, h, l, c):
    m, s, _ = talib.MACD(c, 12, 26, 9)
    up, dn = _cross_up(m, s), _cross_up(s, m)
    return Signals(np.where(up, 1, np.where(dn, -1, 0)), dn, up, flip=True)


def s_ema_cross(o, h, l, c):
    f, s = talib.EMA(c, 20), talib.EMA(c, 50)
    up, dn = _cross_up(f, s), _cross_up(s, f)
    return Signals(np.where(up, 1, np.where(dn, -1, 0)), dn, up, flip=True)


def s_donchian(o, h, l, c):
    hi20, lo20 = _shift(talib.MAX(h, 20)), _shift(talib.MIN(l, 20))
    hi10, lo10 = _shift(talib.MAX(h, 10)), _shift(talib.MIN(l, 10))
    long_, short = c > hi20, c < lo20
    return Signals(np.where(long_, 1, np.where(short, -1, 0)), c < lo10, c > hi10)


def s_adx_trend(o, h, l, c):
    adx, f, s = talib.ADX(h, l, c, 14), talib.EMA(c, 20), talib.EMA(c, 50)
    long_ = (adx > 25) & (f > s)
    short = (adx > 25) & (f < s)
    return Signals(np.where(long_, 1, np.where(short, -1, 0)), (adx < 20) | (f < s), (adx < 20) | (f > s))


def s_trap_tight_tp(o, h, l, c):
    base = s_rsi14_reversion(o, h, l, c)
    never = np.zeros(len(c), dtype=bool)
    return Signals(base.entry, never, never, tp=0.005, sl=0.05, max_hold=200)


STRATEGIES = {
    "RSI14 reversion": s_rsi14_reversion,
    "Connors RSI2": s_connors_rsi2,
    "Bollinger reversion": s_bollinger_reversion,
    "MACD cross (always in)": s_macd_cross,
    "EMA 20/50 cross": s_ema_cross,
    "Donchian 20/10 breakout": s_donchian,
    "ADX>25 + EMA trend": s_adx_trend,
    "TRAP: TP 0.5% / SL 5%": s_trap_tight_tp,
}


# ----------------------------------------------------------------- simulator
def simulate(o, h, l, c, sig: Signals, start: int, end: int, fee: float = FEE_PER_SIDE) -> list[dict]:
    trades = []
    pos, entry_px, entry_t = 0, 0.0, 0
    pending_exit, pending_entry = False, 0

    def close(t, px, why):
        nonlocal pos
        trades.append({"dir": pos, "entry_t": entry_t, "exit_t": t, "entry_px": entry_px, "exit_px": px,
                       "net": pos * (px / entry_px - 1) - 2 * fee, "why": why})
        pos = 0

    for t in range(start, end):
        # 1) fills at this bar's open for decisions made on the previous close
        if pos and pending_exit:
            close(t, o[t], "signal")
        pending_exit = False
        if not pos and pending_entry:
            pos, entry_px, entry_t = pending_entry, o[t], t
        pending_entry = 0

        # 2) intrabar TP/SL (stop first if both touched)
        if pos and (sig.tp or sig.sl):
            if pos > 0:
                sl_px = entry_px * (1 - sig.sl) if sig.sl else None
                tp_px = entry_px * (1 + sig.tp) if sig.tp else None
                if sl_px is not None and l[t] <= sl_px:
                    close(t, min(o[t], sl_px), "stop")
                elif tp_px is not None and h[t] >= tp_px:
                    close(t, max(o[t], tp_px), "target")
            else:
                sl_px = entry_px * (1 + sig.sl) if sig.sl else None
                tp_px = entry_px * (1 - sig.tp) if sig.tp else None
                if sl_px is not None and h[t] >= sl_px:
                    close(t, max(o[t], sl_px), "stop")
                elif tp_px is not None and l[t] <= tp_px:
                    close(t, min(o[t], tp_px), "target")

        if t == end - 1:
            break  # nothing can fill after the last bar
        # 3) decisions on this bar's close
        e = int(sig.entry[t])
        if pos:
            ex = sig.exit_long[t] if pos > 0 else sig.exit_short[t]
            timed_out = sig.max_hold is not None and (t - entry_t + 1) >= sig.max_hold
            if ex or timed_out or (sig.flip and e == -pos):
                pending_exit = True
                if sig.flip and e == -pos:
                    pending_entry = e
        elif e:
            pending_entry = e

    if pos:
        close(end - 1, c[end - 1], "end")
    return trades


def summarize(trades: list[dict]) -> dict | None:
    if not trades:
        return None
    rs = [t["net"] for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in rs:
        eq *= 1 + r
        peak = max(peak, eq)
        mdd = max(mdd, 1 - eq / peak)
    gl = -sum(losses)
    return {
        "n": len(rs),
        "win": 100 * len(wins) / len(rs),
        "avg_win_bps": 1e4 * statistics.fmean(wins) if wins else 0.0,
        "avg_loss_bps": 1e4 * statistics.fmean(losses) if losses else 0.0,
        "exp_bps": 1e4 * statistics.fmean(rs),
        "pf": sum(wins) / gl if gl > 0 else math.inf,
        "ret": 100 * (eq - 1),
        "mdd": 100 * mdd,
    }


def run(coins, interval, bars, fee):
    results = {}   # (strategy, half) -> {"trades": [...], "per_coin": {coin: summary}}
    bh = {}
    for coin in coins:
        df = T.ohlcv_frame(hl_client.candles(coin, interval, bars))
        o, h, l, c = (df[k].to_numpy(dtype="float64") for k in ("open", "high", "low", "close"))
        n = len(df)
        split = n // 2
        bh[coin] = {"IS": 100 * (c[split - 1] / o[0] - 1), "OOS": 100 * (c[-1] / o[split] - 1),
                    "span": (df.index[0], df.index[split], df.index[-1])}
        for name, fn in STRATEGIES.items():
            sig = fn(o, h, l, c)
            for half, (a, b) in (("IS", (0, split)), ("OOS", (split, n))):
                tr = simulate(o, h, l, c, sig, a, b, fee)
                slot = results.setdefault((name, half), {"trades": [], "per_coin": {}})
                slot["trades"] += tr
                slot["per_coin"][coin] = summarize(tr)
    return results, bh


def report(results, bh, coins, interval, fee):
    print(f"Strategy backtest | {interval} | fee {fee:.3%}/side | fill next open | SL-first on ambiguous bars\n")
    for coin in coins:
        s = bh[coin]["span"]
        print(f"{coin}: IS {s[0]:%Y-%m-%d}..{s[1]:%Y-%m-%d} buy&hold {bh[coin]['IS']:+.1f}% | "
              f"OOS {s[1]:%Y-%m-%d}..{s[2]:%Y-%m-%d} buy&hold {bh[coin]['OOS']:+.1f}%")
    rows = []
    for name in STRATEGIES:
        si = summarize(results[(name, "IS")]["trades"])
        so = summarize(results[(name, "OOS")]["trades"])
        rets = {h: [results[(name, h)]["per_coin"][c]["ret"] if results[(name, h)]["per_coin"][c] else 0.0
                    for c in coins] for h in ("IS", "OOS")}
        rows.append((name, si, so, rets))
    rows.sort(key=lambda r: -(r[2]["win"] if r[2] else -1))

    print("\nPooled across coins, sorted by OUT-OF-SAMPLE win rate. exp = expectancy per trade, net of fees.")
    print(f"{'strategy':26}| {'IS n':>5} {'win%':>5} {'exp':>6} {'PF':>5} | {'OOS n':>5} {'win%':>5} "
          f"{'avgW':>6} {'avgL':>7} {'exp':>6} {'PF':>5} | flags")
    for name, si, so, _ in rows:
        flags = []
        for half, s in (("IS", si), ("OOS", so)):
            if s and s["win"] >= 60 and s["exp_bps"] < 0:
                flags.append(f"HIGH-WIN/NEG-EXP:{half}")
        if so and so["n"] < MIN_N:
            flags.append("LOW-N")
        if so and si and (so["exp_bps"] > 0) != (si["exp_bps"] > 0):
            flags.append("SIGN-FLIP IS->OOS")
        print(f"{name:26}| {si['n']:5d} {si['win']:5.1f} {si['exp_bps']:+6.1f} {si['pf']:5.2f} | "
              f"{so['n']:5d} {so['win']:5.1f} {so['avg_win_bps']:+6.0f} {so['avg_loss_bps']:+7.0f} "
              f"{so['exp_bps']:+6.1f} {so['pf']:5.2f} | {' '.join(flags)}")

    print("\nCompounded return per coin, 1x, full equity (IS -> OOS); max drawdown is OOS:")
    hdr = "".join(f"{c:>22}" for c in coins)
    print(f"{'strategy':26}{hdr}")
    for name, _, _, rets in rows:
        cells = ""
        for i, c in enumerate(coins):
            pc = results[(name, "OOS")]["per_coin"][c]
            dd = pc["mdd"] if pc else 0.0
            cells += f"{rets['IS'][i]:+7.1f} -> {rets['OOS'][i]:+6.1f} dd{dd:4.0f}".rjust(22)
        print(f"{name:26}{cells}")
    bhcells = "".join(f"{bh[c]['IS']:+7.1f} -> {bh[c]['OOS']:+6.1f}      ".rjust(22) for c in coins)
    print(f"{'BUY & HOLD':26}{bhcells}")
    print("\nNot simulated: grid / martingale (rare-blow-up risk invisible in short samples). Funding ignored.")
    print("Past results on a few months of data do not guarantee future results. Analysis only.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--coins", nargs="+", default=["BTC", "ETH", "SOL"])
    ap.add_argument("--interval", default="1h", choices=sorted(hl_client.INTERVAL_MS))
    ap.add_argument("--bars", type=int, default=5000)
    ap.add_argument("--fee", type=float, default=FEE_PER_SIDE)
    a = ap.parse_args(argv)
    results, bh = run(a.coins, a.interval, a.bars, a.fee)
    report(results, bh, a.coins, a.interval, a.fee)
    return 0


if __name__ == "__main__":
    sys.exit(main())
