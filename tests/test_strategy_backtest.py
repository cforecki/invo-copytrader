"""Simulator tests on hand-built price arrays (TEST FIXTURES, not market data)."""

import numpy as np
import pytest

pytest.importorskip("talib")
import strategy_backtest as SB  # noqa: E402


def arr(*xs):
    return np.array(xs, dtype="float64")


def sig(entry, exit_long=None, exit_short=None, **kw):
    n = len(entry)
    z = np.zeros(n, dtype=bool)
    return SB.Signals(np.array(entry), z if exit_long is None else np.array(exit_long),
                      z if exit_short is None else np.array(exit_short), **kw)


O = arr(100, 101, 102, 103, 104, 105, 106)
H = O + 0.5
L = O - 0.5
C = O + 0.2


def test_entry_and_exit_fill_at_next_open():
    s = sig([0, 1, 0, 0, 0, 0, 0], exit_long=[0, 0, 0, 1, 0, 0, 0])
    tr = SB.simulate(O, H, L, C, s, 0, len(O), fee=0.0)
    assert len(tr) == 1
    t = tr[0]
    assert (t["entry_t"], t["entry_px"]) == (2, 102.0)   # signal on close of bar 1 -> open of bar 2
    assert (t["exit_t"], t["exit_px"]) == (4, 104.0)     # exit signal close of 3 -> open of 4
    assert t["net"] == pytest.approx(104 / 102 - 1)


def test_fees_charged_both_sides():
    s = sig([0, 1, 0, 0, 0, 0, 0], exit_long=[0, 0, 0, 1, 0, 0, 0])
    tr = SB.simulate(O, H, L, C, s, 0, len(O), fee=0.001)
    assert tr[0]["net"] == pytest.approx(104 / 102 - 1 - 0.002)


def test_stop_assumed_first_when_bar_touches_both():
    o = arr(100, 100, 100, 100)
    h = arr(100, 100, 106, 100)   # bar 2 touches +6% ...
    lo = arr(100, 100, 94, 100)   # ... and -6%
    c = arr(100, 100, 100, 100)
    s = sig([1, 0, 0, 0], tp=0.05, sl=0.05)
    tr = SB.simulate(o, h, lo, c, s, 0, 4, fee=0.0)
    assert tr and tr[0]["why"] == "stop" and tr[0]["exit_px"] == pytest.approx(95.0)


def test_take_profit_alone_exits_at_target_price():
    o = arr(100, 100, 100, 100)
    h = arr(100, 100, 100.7, 100)
    lo = arr(100, 99.9, 99.8, 100)
    c = arr(100, 100, 100, 100)
    s = sig([1, 0, 0, 0], tp=0.005, sl=0.05)
    tr = SB.simulate(o, h, lo, c, s, 0, 4, fee=0.0)
    assert tr and tr[0]["why"] == "target" and tr[0]["exit_px"] == pytest.approx(100.5)


def test_gap_through_stop_fills_at_worse_open():
    o = arr(100, 100, 90, 90)
    h = arr(100, 100, 91, 90)
    lo = arr(100, 99, 89, 90)
    c = arr(100, 100, 90, 90)
    tr = SB.simulate(o, h, lo, c, sig([1, 0, 0, 0], sl=0.05), 0, 4, fee=0.0)
    assert tr and tr[0]["exit_px"] == pytest.approx(90.0)   # not the 95 stop level


def test_no_overlapping_positions_and_max_hold():
    n = 12
    o = np.linspace(100, 111, n)
    s = sig([1] * n, max_hold=3)
    tr = SB.simulate(o, o + 0.1, o - 0.1, o, s, 0, n, fee=0.0)
    assert len(tr) >= 2
    for a, b in zip(tr, tr[1:]):
        assert a["exit_t"] <= b["entry_t"]
    assert all(t["exit_t"] - t["entry_t"] == 3 for t in tr if t["why"] == "signal")


def test_flip_reverses_position():
    s = sig([1, 0, -1, 0, 0, 0, 0], flip=True)
    tr = SB.simulate(O, H, L, C, s, 0, len(O), fee=0.0)
    assert [t["dir"] for t in tr] == [1, -1]
    assert tr[0]["exit_t"] == tr[1]["entry_t"] == 3


def test_summarize_compounds_and_expectancy_consistent():
    trades = [{"net": 0.10}, {"net": -0.05}, {"net": 0.02}]
    s = SB.summarize(trades)
    assert s["ret"] == pytest.approx(100 * (1.10 * 0.95 * 1.02 - 1))
    assert s["mdd"] == pytest.approx(5.0)
    w = s["win"] / 100
    assert w * s["avg_win_bps"] + (1 - w) * s["avg_loss_bps"] == pytest.approx(s["exp_bps"])


def test_all_strategies_produce_aligned_signals():
    rng = np.random.default_rng(0)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 600)))
    o = np.r_[c[0], c[:-1]]
    h, lo = np.maximum(o, c) * 1.002, np.minimum(o, c) * 0.998
    for name, fn in SB.STRATEGIES.items():
        s = fn(o, h, lo, c)
        assert len(s.entry) == len(s.exit_long) == len(s.exit_short) == 600, name
        tr = SB.simulate(o, h, lo, c, s, 0, 600)
        assert tr, f"{name} produced no trades on 600 random-walk bars"
