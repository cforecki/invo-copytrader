#!/usr/bin/env python3
"""Trend-following research: wide universe, long history, volatility-scaled sizing, funding costs.

  python trend_research.py                    # 12 coins, 1d bars, funding on
  python trend_research.py --interval 4h
  python trend_research.py --no-funding       # compare without funding costs

ANALYSIS ONLY -- nothing here trades. The goal is to judge ROBUSTNESS, not to find
a best number:
  - Fixed textbook parameters are the headline. A parameter sweep is reported as a
    DISTRIBUTION (median / share positive / worst), never as a picked winner.
  - Results are broken down per calendar year and per coin, to show whether any edge
    is broad or concentrated in one coin or one year.

Mechanics (reusing strategy_backtest.simulate: next-open fills, fees both sides):
  - Portfolio = equal 1/N sleeves, one per coin. A sleeve holds cash until its coin
    has data, and between trades.
  - Position weight = min(TARGET_VOL / realized_vol, CAP), with realized vol from the
    previous VOL_LOOKBACK bar returns known at the signal close (no look-ahead).
  - Funding: Hyperliquid hourly funding (public fundingHistory) charged on the
    position notional while held; longs pay positive funding. Funding data begins
    2023-05; earlier periods carry NO funding cost, so a separate 2023-05+ window
    with full costs is reported.
  - Equity is marked to market every bar, so drawdowns include open losses.

Caveats printed with every report: survivorship (today's still-listed coins), the
pre-2023 candle history predates Hyperliquid mainnet (its origin is not documented),
multiple configs were tested, and past results do not guarantee future results.
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import talib

import hl_client
import strategy_backtest as SB
import ta_features as T

UNIVERSE = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "AVAX", "LINK", "LTC", "ADA", "SUI", "HYPE"]
TARGET_VOL = 0.40
CAP = 1.0
VOL_LOOKBACK = 30
BARS_PER_YEAR = {"1d": 365, "4h": 6 * 365, "1h": 24 * 365, "12h": 2 * 365}
FUNDING_PAGE = 500


# ---------------------------------------------------------------- signals
def s_ema(fast, slow):
    def fn(o, h, l, c):
        f, s = talib.EMA(c, fast), talib.EMA(c, slow)
        up, dn = SB._cross_up(f, s), SB._cross_up(s, f)
        return SB.Signals(np.where(up, 1, np.where(dn, -1, 0)), dn, up, flip=True)
    return fn


def s_donchian(entry, exit_):
    def fn(o, h, l, c):
        hi_e, lo_e = SB._shift(talib.MAX(h, entry)), SB._shift(talib.MIN(l, entry))
        hi_x, lo_x = SB._shift(talib.MAX(h, exit_)), SB._shift(talib.MIN(l, exit_))
        return SB.Signals(np.where(c > hi_e, 1, np.where(c < lo_e, -1, 0)), c < lo_x, c > hi_x)
    return fn


HEADLINE = {
    "EMA 20/50 cross": s_ema(20, 50),
    "Donchian 20/10": s_donchian(20, 10),
    "ADX>25 + EMA trend": SB.s_adx_trend,
}
SWEEP = {**{f"EMA {f}/{s}": s_ema(f, s) for f in (10, 20, 30) for s in (50, 100, 200)},
         **{f"Donchian {e}/{x}": s_donchian(e, x) for e in (20, 55) for x in (10, 20)}}


# ---------------------------------------------------------------- sizing
def vol_weight(c: np.ndarray, t: int, bars_per_year: int, target=TARGET_VOL, cap=CAP,
               lookback=VOL_LOOKBACK) -> float:
    """Weight for a position decided at the CLOSE of bar t, using returns of bars <= t only."""
    if t < lookback:
        return 0.0
    window = c[t - lookback: t + 1]
    rets = np.diff(np.log(window))
    vol = float(np.std(rets, ddof=1)) * math.sqrt(bars_per_year)
    if not math.isfinite(vol) or vol <= 0:
        return 0.0
    return min(target / vol, cap)


# ---------------------------------------------------------------- funding
class Funding:
    """Cumulative hourly funding for one coin; sum over any [t0, t1) in O(log n)."""

    def __init__(self, rows: list[tuple[int, float]]):
        rows = sorted(rows)
        self.times = [t for t, _ in rows]
        self.cum = [0.0]
        for _, r in rows:
            self.cum.append(self.cum[-1] + r)

    def between(self, t0_ms: int, t1_ms: int) -> float:
        if t1_ms <= t0_ms or not self.times:
            return 0.0
        i0 = bisect.bisect_left(self.times, t0_ms)
        i1 = bisect.bisect_left(self.times, t1_ms)
        return self.cum[i1] - self.cum[i0]

    @property
    def start_ms(self):
        return self.times[0] if self.times else None


def fetch_funding(coin: str, cache_dir: Path, info=None, sleep_s: float = 0.25, now_ms: int | None = None):
    """Page Hyperliquid fundingHistory (500 rows/call), caching to <cache_dir>/<coin>.json incrementally."""
    info = info or hl_client._info
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{coin}.json"
    rows: list[tuple[int, float]] = []
    if path.exists():
        rows = [tuple(r) for r in json.loads(path.read_text())]
    now_ms = now_ms or int(time.time() * 1000)
    cursor = (rows[-1][0] + 1) if rows else 0
    while cursor < now_ms:
        page = info({"type": "fundingHistory", "coin": coin, "startTime": cursor})
        if not page:
            break
        new = [(int(p["time"]), float(p["fundingRate"])) for p in page]
        rows.extend(r for r in new if not rows or r[0] > rows[-1][0])
        last = max(r[0] for r in new)
        if len(page) < FUNDING_PAGE or last < cursor:
            break
        cursor = last + 1
        if sleep_s:
            time.sleep(sleep_s)
    path.write_text(json.dumps(rows))
    return rows


# ---------------------------------------------------------------- sleeve equity
@dataclass
class Sleeve:
    coin: str
    times: np.ndarray       # bar open ms
    equity: np.ndarray      # marked to market at each bar close, starts at 1.0
    trades: list            # trade dicts with weight / funding / sleeve_ret added
    in_market: np.ndarray   # bool per bar


def sleeve_equity(o, h, l, c, times_ms, interval_ms, sig, bars_per_year, fee,
                  funding: Funding | None, target=TARGET_VOL, cap=CAP) -> Sleeve:
    trades = SB.simulate(o, h, l, c, sig, 0, len(c), fee)
    n = len(c)
    eq = np.ones(n)
    in_mkt = np.zeros(n, dtype=bool)
    cur = 1.0
    t_prev_end = 0
    out = []
    for tr in trades:
        et, xt, d = tr["entry_t"], tr["exit_t"], tr["dir"]
        w = vol_weight(c, et - 1, bars_per_year, target, cap)   # decided on close of et-1
        eq[t_prev_end:et] = cur                                  # flat stretch
        if w <= 0:
            t_prev_end = et
            continue
        notional = w * cur
        entry_ms = int(times_ms[et])
        for t in range(et, xt):
            close_ms = int(times_ms[t]) + interval_ms
            fund = funding.between(entry_ms, close_ms) if funding else 0.0
            eq[t] = cur + notional * (d * (c[t] / tr["entry_px"] - 1) - fee - d * fund)
            in_mkt[t] = True
        exit_ms = int(times_ms[xt]) + (0 if tr["why"] == "signal" else interval_ms)
        fund = funding.between(entry_ms, exit_ms) if funding else 0.0
        gross = d * (tr["exit_px"] / tr["entry_px"] - 1)
        ret = w * (gross - 2 * fee - d * fund)
        cur = cur * (1 + ret)
        eq[xt] = cur
        if tr["why"] != "signal":
            in_mkt[xt] = True
        t_prev_end = xt + (0 if tr["why"] == "signal" else 1)
        out.append({**tr, "weight": w, "funding": d * fund, "sleeve_ret": ret})
    eq[t_prev_end:] = cur
    return Sleeve("", np.asarray(times_ms), eq, out, in_mkt)


def buy_and_hold(c, times_ms, fee) -> Sleeve:
    """Spot buy-and-hold at weight 1: no funding, one entry fee (benchmark)."""
    eq = (c / c[0]) * (1 - fee)
    return Sleeve("", np.asarray(times_ms), eq, [], np.ones(len(c), dtype=bool))


# ---------------------------------------------------------------- portfolio
def portfolio(sleeves: dict[str, Sleeve]) -> tuple[np.ndarray, np.ndarray]:
    """Equal-weight sleeves on the union timeline; a sleeve is cash (1.0) before its data starts."""
    grid = np.array(sorted(set().union(*[set(s.times.tolist()) for s in sleeves.values()])))
    total = np.zeros(len(grid))
    for s in sleeves.values():
        idx = np.searchsorted(s.times, grid, side="right") - 1
        vals = np.where(idx >= 0, s.equity[np.clip(idx, 0, None)], 1.0)
        total += vals
    return grid, total / len(sleeves)


def metrics(eq: np.ndarray, bars_per_year: int) -> dict:
    rets = eq[1:] / eq[:-1] - 1
    years = len(rets) / bars_per_year
    cagr = (eq[-1] / eq[0]) ** (1 / years) - 1 if years > 0 and eq[-1] > 0 else float("nan")
    sd = float(np.std(rets, ddof=1)) if len(rets) > 1 else 0.0
    down = float(np.sqrt(np.mean(np.minimum(rets, 0) ** 2))) if len(rets) else 0.0
    peak = np.maximum.accumulate(eq)
    return {
        "total": 100 * (eq[-1] / eq[0] - 1),
        "cagr": 100 * cagr,
        "vol": 100 * sd * math.sqrt(bars_per_year),
        "sharpe": float(np.mean(rets)) / sd * math.sqrt(bars_per_year) if sd > 0 else float("nan"),
        "sortino": float(np.mean(rets)) / down * math.sqrt(bars_per_year) if down > 0 else float("nan"),
        "mdd": 100 * float(np.max(1 - eq / peak)),
        "years": years,
    }


def yearly(grid_ms: np.ndarray, eq: np.ndarray) -> dict[int, float]:
    years = np.array([time.gmtime(t / 1000).tm_year for t in grid_ms])
    out = {}
    prev_end = eq[0]
    for y in sorted(set(years.tolist())):
        last = eq[np.where(years == y)[0][-1]]
        out[y] = 100 * (last / prev_end - 1)
        prev_end = last
    return out


# ---------------------------------------------------------------- driver
MIN_PLAUSIBLE_MS = 1_420_070_400_000  # 2015-01-01: anything earlier means a unit bug, not data


def bar_times_ms(df) -> np.ndarray:
    """Bar-open times in epoch ms, independent of pandas' internal datetime unit (ns/us/ms)."""
    t = df.index.as_unit("ms").asi8.astype("int64")
    if len(t) and t[0] < MIN_PLAUSIBLE_MS:
        raise ValueError(f"implausible bar time {t[0]} ms -- timestamp unit conversion is wrong")
    return t


def load_market(coins, interval, bars):
    data = {}
    for coin in coins:
        df = T.ohlcv_frame(hl_client.candles(coin, interval, bars))
        times = bar_times_ms(df)
        data[coin] = (df["open"].to_numpy(), df["high"].to_numpy(), df["low"].to_numpy(),
                      df["close"].to_numpy(), times)
    return data


def run_strategy(fn, data, interval, fee, fundings):
    bpy = BARS_PER_YEAR[interval]
    ims = hl_client.INTERVAL_MS[interval]
    sleeves = {}
    for coin, (o, h, l, c, t) in data.items():
        sleeves[coin] = sleeve_equity(o, h, l, c, t, ims, fn(o, h, l, c), bpy, fee, fundings.get(coin))
        sleeves[coin].coin = coin
    return sleeves


def window(grid, eq, start_ms):
    i = int(np.searchsorted(grid, start_ms))
    return grid[i:], eq[i:]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--coins", nargs="+", default=UNIVERSE)
    ap.add_argument("--interval", default="1d", choices=sorted(BARS_PER_YEAR))
    ap.add_argument("--bars", type=int, default=5000)
    ap.add_argument("--fee", type=float, default=SB.FEE_PER_SIDE)
    ap.add_argument("--no-funding", action="store_true")
    ap.add_argument("--funding-cache", default="data/funding")
    a = ap.parse_args(argv)
    bpy = BARS_PER_YEAR[a.interval]

    data = load_market(a.coins, a.interval, a.bars)
    fundings = {}
    if not a.no_funding:
        for coin in a.coins:
            fundings[coin] = Funding(fetch_funding(coin, Path(a.funding_cache)))
    fstart = min((f.start_ms for f in fundings.values() if f.start_ms), default=None)

    print(f"Trend research | {a.interval} | {len(a.coins)} coins | fee {a.fee:.3%}/side | "
          f"vol target {TARGET_VOL:.0%}/sleeve, cap {CAP:.1f}x | funding {'ON' if fundings else 'OFF'}")
    for coin, (_, _, _, c, t) in data.items():
        print(f"  {coin:5} {time.strftime('%Y-%m-%d', time.gmtime(t[0]/1000))} .. "
              f"{time.strftime('%Y-%m-%d', time.gmtime(t[-1]/1000))}  ({len(c)} bars)")

    bench = {coin: buy_and_hold(c, t, a.fee) for coin, (_, _, _, c, t) in data.items()}
    g_bh, e_bh = portfolio(bench)
    btc = {"BTC": bench["BTC"]} if "BTC" in bench else None

    results = {}
    for name, fn in HEADLINE.items():
        sl = run_strategy(fn, data, a.interval, a.fee, fundings)
        results[name] = (sl, *portfolio(sl))

    def row(label, grid, eq, extra=""):
        m = metrics(eq, bpy)
        print(f"{label:24} {m['total']:+9.1f}% {m['cagr']:+7.1f}% {m['vol']:6.1f}% "
              f"{m['sharpe']:+6.2f} {m['sortino']:+6.2f} {m['mdd']:6.1f}%  {extra}")

    for title, start in (("FULL HISTORY", None), ("2023-05+ (funding data available)", fstart)):
        if start is None and title.startswith("2023"):
            continue
        print(f"\n=== {title} ===")
        print(f"{'portfolio':24} {'total':>10} {'CAGR':>8} {'vol':>7} {'Sharpe':>6} {'Sortino':>7} {'maxDD':>7}")
        for name, (sl, g, e) in results.items():
            gg, ee = (g, e) if start is None else window(g, e, start)
            trs = [tr for s in sl.values() for tr in s.trades
                   if start is None or s.times[tr["entry_t"]] >= start]
            st = SB.summarize([{"net": tr["sleeve_ret"]} for tr in trs])
            fund = sum(tr["weight"] * tr["funding"] for tr in trs)
            tim = np.mean([s.in_market.mean() for s in sl.values()])
            extra = (f"trades {st['n']}, win {st['win']:.0f}%, in-mkt {100*tim:.0f}%, "
                     f"funding paid {100*fund/len(sl):+.1f}%/sleeve" if st else "")
            row(name, gg, ee, extra)
        gg, ee = (g_bh, e_bh) if start is None else window(g_bh, e_bh, start)
        row("EW buy&hold (spot)", gg, ee)
        if btc:
            gb, eb = portfolio(btc)
            gb, eb = (gb, eb) if start is None else window(gb, eb, start)
            row("BTC buy&hold (spot)", gb, eb)

    # per coin
    print("\n=== PER COIN: sleeve total return, full history (trend strategies are vol-scaled, B&H is 1x) ===")
    print(f"{'coin':6}" + "".join(f"{n[:18]:>20}" for n in results) + f"{'buy&hold':>12}")
    pos = {n: 0 for n in results}
    for coin in data:
        cells = ""
        for n, (sl, _, _) in results.items():
            r = 100 * (sl[coin].equity[-1] - 1)
            pos[n] += r > 0
            cells += f"{r:+19.1f}%"
        print(f"{coin:6}{cells}{100*(bench[coin].equity[-1]-1):+11.1f}%")
    print(f"{'#pos':6}" + "".join(f"{f'{pos[n]}/{len(data)}':>20}" for n in results))

    # per year
    print("\n=== PER CALENDAR YEAR: portfolio return ===")
    yrs = {n: yearly(g, e) for n, (_, g, e) in results.items()}
    ybh = yearly(g_bh, e_bh)
    print(f"{'year':6}" + "".join(f"{n[:18]:>20}" for n in results) + f"{'EW buy&hold':>14}")
    for y in sorted(ybh):
        print(f"{y:<6}" + "".join(f"{yrs[n].get(y, float('nan')):+19.1f}%" for n in results) + f"{ybh[y]:+13.1f}%")

    # sweep distribution
    print(f"\n=== PARAMETER SWEEP ({len(SWEEP)} configs) -- distribution, NOT a pick ===")
    sweep = []
    for name, fn in SWEEP.items():
        sl = run_strategy(fn, data, a.interval, a.fee, fundings)
        g, e = portfolio(sl)
        m_recent = metrics(window(g, e, fstart)[1], bpy) if fstart else None
        sweep.append((name, metrics(e, bpy), m_recent))
    windows = [("full history", 1)] + ([("2023-05+", 2)] if fstart else [])
    for label, k in windows:
        sh = [x[k]["sharpe"] for x in sweep]
        worst = min(sweep, key=lambda x: x[k]["sharpe"])
        print(f"{label:13} Sharpe median {statistics.median(sh):+.2f}, min {min(sh):+.2f}, max {max(sh):+.2f}; "
              f"positive total return {sum(1 for x in sweep if x[k]['total'] > 0)}/{len(sweep)}; "
              f"worst {worst[0]} (Sharpe {worst[k]['sharpe']:+.2f}, maxDD {worst[k]['mdd']:.0f}%)")
    for name, m, mr in sorted(sweep, key=lambda x: x[0]):
        recent = f" | 2023-05+: Sharpe {mr['sharpe']:+.2f}  CAGR {mr['cagr']:+6.1f}%  maxDD {mr['mdd']:5.1f}%" if mr else ""
        print(f"  {name:16} Sharpe {m['sharpe']:+.2f}  CAGR {m['cagr']:+6.1f}%  maxDD {m['mdd']:5.1f}%{recent}")

    n_cfg = len(HEADLINE) + len(SWEEP)
    print(f"\nCaveats: {n_cfg} configurations tested -> the best-looking one is biased upward; a Sharpe t-stat "
          f"~ Sharpe*sqrt(years) is optimistic here. Universe = coins still listed today (survivorship). "
          f"Candles before 2023 predate Hyperliquid mainnet (origin undocumented). "
          f"{'Funding before ' + time.strftime('%Y-%m', time.gmtime(fstart/1000)) + ' not charged. ' if fstart else ''}"
          f"Slippage beyond fees not modelled. Past results do not guarantee future results. Analysis only.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
