import math

import pytest

import metrics as M
from conftest import NOW, inv


def trades(raws):
    return [M.trade_from_invo(r) for r in raws]


def test_margin_and_account_return_hand_computed():
    t = M.trade_from_invo(inv(1, entry=100, exit_=110, lev=3, size=10))
    # 3x * +10% = +30% on margin, minus 2 * 0.045% * 3 fees
    assert t.margin_return == pytest.approx(0.30 - 2 * M.FEE_PER_SIDE * 3)
    assert t.account_return == pytest.approx(0.10 * t.margin_return)


def test_short_and_liquidation_floor():
    short = M.trade_from_invo(inv(1, long=False, entry=100, exit_=90, lev=2))
    assert short.margin_return == pytest.approx(0.20 - 2 * M.FEE_PER_SIDE * 2)
    liq = M.trade_from_invo(inv(2, entry=100, exit_=40, lev=10))
    assert liq.margin_return == -1.0


def test_profit_factor_win_rate_drawdown():
    raws = [
        inv(1, entry=100, exit_=110, lev=1, size=100, opened_days_ago=40),  # +10%
        inv(2, entry=100, exit_=95, lev=1, size=100, opened_days_ago=30),   # -5%
        inv(3, entry=100, exit_=90, lev=1, size=100, opened_days_ago=20),   # -10%
        inv(4, entry=100, exit_=120, lev=1, size=100, opened_days_ago=10),  # +20%
    ]
    m = M.compute_metrics(trades(raws), now=NOW)
    f = 2 * M.FEE_PER_SIDE
    r = [0.10 - f, -0.05 - f, -0.10 - f, 0.20 - f]
    assert m.win_rate_pct == 50
    assert m.profit_factor == pytest.approx((r[0] + r[3]) / -(r[1] + r[2]))
    eq = [1.0]
    for x in r:
        eq.append(eq[-1] * (1 + x))
    peak = eq[1]
    assert m.max_drawdown_pct == pytest.approx(100 * (1 - eq[3] / peak))
    assert m.total_return_pct == pytest.approx(100 * (eq[-1] - 1))
    assert m.cagr_pct is None  # < 90 days: not annualized


def test_missing_size_disables_account_metrics():
    raws = [inv(1, exit_=110, size=None), inv(2, exit_=90)]
    raws[0].pop("entrySize")
    m = M.compute_metrics(trades(raws), now=NOW)
    assert m.max_drawdown_pct is None and m.total_return_pct is None
    assert any("SIZE_UNKNOWN" in w for w in m.warnings)
    assert m.profit_factor is not None  # equal-margin proxy still reported


def test_open_position_counts_in_current_drawdown():
    closed = trades([inv(1, entry=100, exit_=110, lev=1, size=50, opened_days_ago=5)])
    open_ = trades([inv(2, coin="ETH", entry=100, lev=2, size=50, is_open=True, opened_days_ago=1)])
    m = M.compute_metrics(closed, open_, mids={"ETH": 80.0}, now=NOW)
    # ETH long 2x at -20% -> -40% on 50% of equity -> -20% of equity
    assert m.current_drawdown_pct == pytest.approx(20.0, abs=0.01)
    assert m.max_drawdown_pct >= m.current_drawdown_pct


def test_martingale_adds_detected():
    raws = [
        inv(1, entry=100, exit_=105, opened_days_ago=10, hold_days=5),
        inv(2, entry=95, exit_=105, opened_days_ago=8, hold_days=3),   # added lower while #1 open
        inv(3, entry=90, exit_=105, opened_days_ago=7, hold_days=2),   # again
        inv(4, coin="ETH", entry=100, exit_=101, opened_days_ago=3),
    ]
    m = M.compute_metrics(trades(raws), now=NOW)
    assert m.martingale_add_pct == 50.0


def test_no_stop_loss_and_concentration():
    raws = [inv(i, exit_=101, sl=None, opened_days_ago=20 - i) for i in range(8)]
    raws += [inv(9, coin="ETH", exit_=101, opened_days_ago=2)]
    m = M.compute_metrics(trades(raws), now=NOW)
    assert m.no_stop_loss_pct == pytest.approx(800 / 9)
    assert m.top_coin == "BTC" and m.top_coin_share_pct == pytest.approx(800 / 9)


def test_sharpe_needs_enough_history():
    short = M.compute_metrics(trades([inv(1, exit_=110)]), now=NOW)
    assert short.sharpe is None
    raws = [inv(i, entry=100, exit_=101 + (i % 3) - 1.2, size=5, opened_days_ago=100 - 3 * i) for i in range(30)]
    long_ = M.compute_metrics(trades(raws), now=NOW)
    assert long_.sharpe is not None and math.isfinite(long_.sharpe)
    assert long_.cagr_pct is not None


def test_timestamp_formats():
    assert M.parse_ts("2026-01-01T00:00:00Z").year == 2026
    assert M.parse_ts(1767225600000).year == 2026  # epoch ms
    assert M.parse_ts("garbage") is None
