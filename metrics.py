"""Trader performance metrics reconstructed from Invo trade history.

How returns are reconstructed (read this before trusting any number):

  Invo exposes per trade: entryPrice, closingPrice, leverage, direction,
  and entrySize = margin as a PERCENT of the trader's own balance
  (e.g. 2.5 -> 2.5%; semantics confirmed by invo-sentinel against the app
  UI, not documented by Invo). So a trade's contribution to the trader's
  account return is

      account_ret = entrySize/100 * leverage * dir * (exit/entry - 1)
                    - entrySize/100 * leverage * 2 * FEE_PER_SIDE

  capped at -entrySize/100 (you can't lose more than the margin; that's a
  liquidation). Trades are compounded in close-time order into a REALIZED
  equity curve.

Known blind spots (surfaced in the report, not hidden):
  - Intra-trade drawdown is invisible: a trade that went -60% then closed
    +5% shows as +5%. Realized max drawdown therefore UNDERSTATES real
    drawdown. Current drawdown does include open positions' unrealized PnL.
  - Funding payments are ignored; fees are an assumption (FEE_PER_SIDE).
  - Partial closes/resizes: entrySize is the last snapshot Invo stored.
  - If entrySize is missing on any trade, account-level metrics (return,
    CAGR, drawdown, Sharpe) are reported as None rather than guessed.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

FEE_PER_SIDE = 0.00045       # Hyperliquid base taker fee; Invo may add its own
MIN_DAYS_FOR_CAGR = 90       # annualizing a shorter record is mostly noise
MIN_DAYS_FOR_SHARPE = 60
MIN_TRADES_FOR_SHARPE = 20
NO_SL_THRESHOLD = 0.5        # >50% of trades without a stop -> flag
CONCENTRATION_THRESHOLD = 0.7
MARTINGALE_ADD_THRESHOLD = 0.10
SIZE_ESCALATION_MULT = 1.5
SIZE_ESCALATION_THRESHOLD = 0.4
HIGH_MAX_LEVERAGE = 25


def parse_ts(v) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        secs = v / 1000 if v > 1e11 else v
        return datetime.fromtimestamp(secs, tz=timezone.utc)
    s = str(v).strip()
    if s.isdigit():
        return parse_ts(int(s))
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _f(v) -> float | None:
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


@dataclass
class Trade:
    trade_id: str
    coin: str
    is_long: bool
    leverage: float
    size_pct: float | None          # margin as % of trader balance
    entry_price: float
    exit_price: float | None        # None for open trades
    opened_at: datetime
    closed_at: datetime | None
    stop_loss: float | None
    reason_closed: str | None = None

    @property
    def margin_return(self) -> float | None:
        """Return on margin, net of assumed fees, floored at -100%."""
        if self.exit_price is None or not self.entry_price:
            return None
        d = 1 if self.is_long else -1
        r = d * (self.exit_price / self.entry_price - 1) * self.leverage
        r -= 2 * FEE_PER_SIDE * self.leverage
        return max(r, -1.0)

    @property
    def account_return(self) -> float | None:
        mr = self.margin_return
        if mr is None or self.size_pct is None:
            return None
        return self.size_pct / 100 * mr

    @property
    def hold_hours(self) -> float | None:
        if self.closed_at is None:
            return None
        return (self.closed_at - self.opened_at).total_seconds() / 3600


def trade_from_invo(inv: dict) -> Trade | None:
    """Normalize a raw Invo investment. Returns None if it lacks price/time data."""
    entry = _f(inv.get("entryPrice"))
    opened = parse_ts(inv.get("createdAt"))
    if not entry or opened is None:
        return None
    is_open = bool(inv.get("isOpen"))
    exit_px = None if is_open else _f(inv.get("closingPrice"))
    closed = None if is_open else parse_ts(inv.get("closedAt") or inv.get("updatedAt"))
    if not is_open and (exit_px is None or closed is None):
        return None
    size = _f(inv.get("entrySize"))
    if size is None:
        size = _f(inv.get("positionSize"))
    sl = _f(inv.get("stopLoss"))
    return Trade(
        trade_id=str(inv.get("baseId") or inv.get("id") or ""),
        coin=str(inv.get("ticker") or "").upper(),
        is_long=bool(inv.get("directionLong", True)),
        leverage=_f(inv.get("leverage")) or 1.0,
        size_pct=size if size and size > 0 else None,
        entry_price=entry,
        exit_price=exit_px,
        opened_at=opened,
        closed_at=closed,
        stop_loss=sl if sl else None,
        reason_closed=inv.get("reasonClosed"),
    )


def _compound(rets) -> float:
    eq = 1.0
    for r in rets:
        eq *= 1 + r
    return eq - 1


def _drawdown(curve: list[float]) -> tuple[float, float]:
    """(max drawdown, current drawdown) as positive fractions."""
    peak, mdd = -math.inf, 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, 1 - v / peak)
    cur = 1 - curve[-1] / peak if curve and peak > 0 else 0.0
    return mdd, cur


@dataclass
class Metrics:
    n_closed: int = 0
    n_open: int = 0
    days_active: float = 0.0
    sized: bool = False
    total_return_pct: float | None = None
    cagr_pct: float | None = None
    win_rate_pct: float | None = None
    profit_factor: float | None = None
    max_drawdown_pct: float | None = None
    current_drawdown_pct: float | None = None
    sharpe: float | None = None
    sortino: float | None = None
    avg_leverage: float | None = None
    max_leverage: float | None = None
    avg_hold_hours: float | None = None
    trades_per_week: float | None = None
    profitable_months_pct: float | None = None
    n_months: int = 0
    return_30d_pct: float | None = None
    return_prior90d_pct: float | None = None
    no_stop_loss_pct: float | None = None
    martingale_add_pct: float | None = None
    loss_size_escalation_pct: float | None = None
    top_coin: str | None = None
    top_coin_share_pct: float | None = None
    warnings: list[str] = field(default_factory=list)


def compute_metrics(
    closed: list[Trade],
    open_: list[Trade] | None = None,
    mids: dict[str, float] | None = None,
    now: datetime | None = None,
) -> Metrics:
    now = now or datetime.now(timezone.utc)
    open_ = open_ or []
    mids = mids or {}
    m = Metrics(n_closed=len(closed), n_open=len(open_))
    all_trades = closed + open_
    if not all_trades:
        m.warnings.append("NO_TRADES")
        return m

    closed = sorted(closed, key=lambda t: t.closed_at)
    first = min(t.opened_at for t in all_trades)
    m.days_active = max((now - first).total_seconds() / 86400, 0.0)

    # --- size-independent trade stats (per-trade return on margin) --------
    mrets = [t.margin_return for t in closed]
    if closed:
        wins = [r for r in mrets if r > 0]
        m.win_rate_pct = 100 * len(wins) / len(closed)
        holds = [t.hold_hours for t in closed if t.hold_hours is not None]
        m.avg_hold_hours = statistics.fmean(holds) if holds else None
        if m.days_active > 0:
            m.trades_per_week = len(closed) / (m.days_active / 7)

    levs = [t.leverage for t in all_trades]
    m.avg_leverage = statistics.fmean(levs)
    m.max_leverage = max(levs)

    # --- account-level stats (need entrySize on every closed trade) -------
    m.sized = bool(closed) and all(t.size_pct is not None for t in closed)
    if closed and m.sized:
        arets = [t.account_return for t in closed]
        gp = sum(r for r in arets if r > 0)
        gl = -sum(r for r in arets if r < 0)
        m.profit_factor = gp / gl if gl > 0 else (math.inf if gp > 0 else None)

        curve = [1.0]
        for r in arets:
            curve.append(curve[-1] * (1 + r))
        m.total_return_pct = 100 * (curve[-1] - 1)
        mdd, _ = _drawdown(curve)
        m.max_drawdown_pct = 100 * mdd

        # current drawdown including open positions marked to market
        unreal = 0.0
        for t in open_:
            px = mids.get(t.coin)
            if px and t.size_pct is not None:
                d = 1 if t.is_long else -1
                unreal += t.size_pct / 100 * max(d * (px / t.entry_price - 1) * t.leverage, -1.0)
        live_eq = curve[-1] * (1 + unreal)
        peak = max(max(curve), live_eq)
        m.current_drawdown_pct = 100 * max(0.0, 1 - live_eq / peak)
        m.max_drawdown_pct = max(m.max_drawdown_pct, m.current_drawdown_pct)

        if m.days_active >= MIN_DAYS_FOR_CAGR and curve[-1] > 0:
            m.cagr_pct = 100 * (curve[-1] ** (365 / m.days_active) - 1)

        # daily returns (zero on no-trade days) for Sharpe / Sortino
        if m.days_active >= MIN_DAYS_FOR_SHARPE and len(closed) >= MIN_TRADES_FOR_SHARPE:
            by_day: dict = defaultdict(list)
            for t, r in zip(closed, arets):
                by_day[t.closed_at.date()].append(r)
            n_days = int(m.days_active) + 1
            start = first.date()
            daily = []
            for i in range(n_days):
                d = start.fromordinal(start.toordinal() + i)
                daily.append(_compound(by_day.get(d, [])))
            mu = statistics.fmean(daily)
            sd = statistics.pstdev(daily)
            downside = math.sqrt(statistics.fmean([min(r, 0) ** 2 for r in daily]))
            m.sharpe = mu / sd * math.sqrt(365) if sd > 0 else None
            m.sortino = mu / downside * math.sqrt(365) if downside > 0 else None

        # monthly consistency (months with at least one closed trade)
        by_month: dict = defaultdict(list)
        for t, r in zip(closed, arets):
            by_month[(t.closed_at.year, t.closed_at.month)].append(r)
        month_rets = [_compound(v) for v in by_month.values()]
        m.n_months = len(month_rets)
        if month_rets:
            m.profitable_months_pct = 100 * sum(1 for r in month_rets if r > 0) / len(month_rets)

        # recent form
        def window(lo_days, hi_days):
            return [r for t, r in zip(closed, arets)
                    if lo_days <= (now - t.closed_at).total_seconds() / 86400 < hi_days]
        m.return_30d_pct = 100 * _compound(window(0, 30))
        if m.days_active >= 60:
            m.return_prior90d_pct = 100 * _compound(window(30, 120))
    elif closed:
        m.warnings.append("SIZE_UNKNOWN: entrySize missing on some trades; return/drawdown/Sharpe not computed")
        # PF on margin returns as an equal-margin proxy, labeled as such
        gp = sum(r for r in mrets if r > 0)
        gl = -sum(r for r in mrets if r < 0)
        m.profit_factor = gp / gl if gl > 0 else (math.inf if gp > 0 else None)

    # --- risk-behaviour flags --------------------------------------------
    m.no_stop_loss_pct = 100 * sum(1 for t in all_trades if t.stop_loss is None) / len(all_trades)

    coins = Counter(t.coin for t in all_trades)
    m.top_coin, top_n = coins.most_common(1)[0]
    m.top_coin_share_pct = 100 * top_n / len(all_trades)

    # averaging down: a new same-coin/same-direction entry at a worse price
    # while an earlier one is still open
    adds = 0
    ordered = sorted(all_trades, key=lambda t: t.opened_at)
    for i, t in enumerate(ordered):
        for prev in ordered[:i]:
            if prev.coin != t.coin or prev.is_long != t.is_long:
                continue
            still_open = prev.closed_at is None or prev.closed_at > t.opened_at
            worse = t.entry_price < prev.entry_price if t.is_long else t.entry_price > prev.entry_price
            if still_open and worse:
                adds += 1
                break
    m.martingale_add_pct = 100 * adds / len(all_trades)

    # size escalation right after a realized loss
    if m.sized and len(closed) >= 2:
        by_open = sorted(all_trades, key=lambda t: t.opened_at)
        losses_followed = escalated = 0
        for lt in closed:
            if lt.margin_return is None or lt.margin_return >= 0:
                continue
            nxt = next((t for t in by_open if t.opened_at > lt.closed_at and t.size_pct), None)
            if nxt is None:
                continue
            losses_followed += 1
            if nxt.size_pct >= SIZE_ESCALATION_MULT * lt.size_pct:
                escalated += 1
        if losses_followed >= 5:
            m.loss_size_escalation_pct = 100 * escalated / losses_followed

    return m
