import metrics as M
import scoring


def good(**kw):
    m = M.Metrics(n_closed=120, n_open=1, days_active=300, sized=True, total_return_pct=60,
                  win_rate_pct=58, profit_factor=2.2, max_drawdown_pct=12, current_drawdown_pct=3,
                  sharpe=1.8, avg_leverage=3, max_leverage=8, profitable_months_pct=80, n_months=10,
                  return_30d_pct=3, return_prior90d_pct=10, no_stop_loss_pct=10, martingale_add_pct=0,
                  top_coin="BTC", top_coin_share_pct=40)
    for k, v in kw.items():
        setattr(m, k, v)
    return m


def test_good_trader_approved():
    r = scoring.score(good())
    assert r.status == "APPROVED", (r.red_flags, r.yellow_flags, r.score)
    assert 60 <= r.score <= 100


def test_red_rules():
    assert "HISTORY<60d" in scoring.score(good(days_active=45)).red_flags
    assert "TRADES<30" in scoring.score(good(n_closed=20)).red_flags
    assert "MDD>40%" in scoring.score(good(max_drawdown_pct=45)).red_flags
    assert "PF<1.2" in scoring.score(good(profit_factor=1.1)).red_flags
    assert "RECENT_CRASH" in scoring.score(good(return_30d_pct=-25, return_prior90d_pct=30)).red_flags
    assert scoring.score(good(max_drawdown_pct=45)).status == "RED"


def test_between_red_and_approval_minimums_is_yellow():
    r = scoring.score(good(days_active=75, n_closed=40))
    assert not r.red_flags and not r.approved and r.status == "YELLOW"


def test_weights_renormalize_and_monotonic():
    a = scoring.score(good(max_drawdown_pct=10)).score
    b = scoring.score(good(max_drawdown_pct=30)).score
    assert a > b
    old = dict(scoring.WEIGHTS)
    try:
        for k in scoring.WEIGHTS:
            scoring.WEIGHTS[k] *= 3
        assert scoring.score(good(max_drawdown_pct=10)).score == a
    finally:
        scoring.WEIGHTS.update(old)


def test_unknown_drawdown_blocks_approval():
    r = scoring.score(good(max_drawdown_pct=None))
    assert "MDD_UNKNOWN" in r.yellow_flags and not r.approved


def test_decay_flag():
    r = scoring.score(good(return_30d_pct=-8, return_prior90d_pct=30))
    assert "DECAY" in r.yellow_flags
