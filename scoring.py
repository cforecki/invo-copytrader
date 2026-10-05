"""Trader scoring: 0-100 weighted score + RED/YELLOW flags.

==========================  TUNE HERE  ==========================
Everything that decides a ranking lives in this block. Weights are
renormalized to sum to 1, so you can edit them freely.
"""

WEIGHTS = {
    "max_drawdown": 0.25,
    "profit_factor": 0.20,
    "consistency": 0.20,
    "track_record": 0.15,
    "recent_form": 0.10,
    "leverage_discipline": 0.10,
}

# Component scales: (value that scores 0, value that scores 100), linear between.
MDD_SCALE = (50.0, 0.0)           # % max drawdown: 50%+ -> 0, 0% -> 100
PF_SCALE = (1.0, 3.0)             # profit factor: <=1.0 -> 0, >=3.0 -> 100
TRACK_DAYS_SCALE = (0.0, 365.0)   # days active
TRACK_TRADES_SCALE = (0.0, 200.0) # closed trades
TRACK_DAYS_WEIGHT = 0.7           # remainder goes to trade count
AVG_LEV_SCALE = (20.0, 3.0)       # avg leverage: 20x+ -> 0, <=3x -> 100
MAX_LEV_SCALE = (50.0, 10.0)      # max leverage: 50x+ -> 0, <=10x -> 100
AVG_LEV_WEIGHT = 0.6              # remainder goes to max leverage
RECENT_FORM_SPREAD = 20.0         # 30d monthly-rate minus prior-90d monthly-rate, in %: +/-20 -> 100/0
CONSISTENCY_FULL_CONF_MONTHS = 6  # fewer months -> shrink consistency toward 50

# RED = auto-excluded. (Spec values.)
RED_MIN_DAYS = 60
RED_MAX_DRAWDOWN_PCT = 40.0
RED_MIN_PROFIT_FACTOR = 1.2
RED_MIN_TRADES = 30
RED_RECENT_CRASH_PCT = -20.0      # 30d return below this while prior 90d was positive

# Approval for the bot's watch list: no RED, these minimums, and score >= MIN_APPROVAL_SCORE.
APPROVE_MIN_DAYS = 90
APPROVE_MIN_TRADES = 50
MIN_APPROVAL_SCORE = 60.0

# YELLOW = warn, still approvable unless listed in BLOCKING_YELLOW.
DECAY_SPREAD_PCT = 5.0            # 30d monthly rate this far below prior-90d monthly rate -> DECAY
BLOCKING_YELLOW = {"MDD_UNKNOWN", "MARTINGALE_ADDS"}
# =================================================================

from dataclasses import dataclass, field  # noqa: E402

import metrics as M  # noqa: E402

DISCLAIMER = (
    "Scores summarize PAST trading activity reconstructed from Invo data and are "
    "not a prediction or a recommendation. Past performance does not guarantee "
    "future results. Copy-trading leveraged perpetuals can lose more than expected, "
    "including through liquidation, slippage and execution lag versus the source trader."
)


def _lin(x: float, zero_at: float, hundred_at: float) -> float:
    if hundred_at == zero_at:
        return 0.0
    t = (x - zero_at) / (hundred_at - zero_at)
    return 100.0 * min(1.0, max(0.0, t))


def _monthly_rate(total_pct: float, months: float) -> float:
    g = 1 + total_pct / 100
    return 100 * (g ** (1 / months) - 1) if g > 0 else -100.0


@dataclass
class ScoreResult:
    score: float
    components: dict
    red_flags: list[str] = field(default_factory=list)
    yellow_flags: list[str] = field(default_factory=list)
    approved: bool = False

    @property
    def status(self) -> str:
        if self.red_flags:
            return "RED"
        if self.approved:
            return "APPROVED" if not self.yellow_flags else "APPROVED*"
        return "YELLOW"


def score(m: M.Metrics) -> ScoreResult:
    c: dict[str, float] = {}

    # Missing data scores 0 for that component -- unknown risk is not neutral.
    c["max_drawdown"] = _lin(m.max_drawdown_pct, *MDD_SCALE) if m.max_drawdown_pct is not None else 0.0
    pf = m.profit_factor
    c["profit_factor"] = 0.0 if pf is None else (100.0 if pf == float("inf") else _lin(pf, *PF_SCALE))

    if m.profitable_months_pct is not None:
        conf = min(m.n_months / CONSISTENCY_FULL_CONF_MONTHS, 1.0)
        c["consistency"] = conf * m.profitable_months_pct + (1 - conf) * 50.0
    else:
        c["consistency"] = 0.0

    c["track_record"] = (TRACK_DAYS_WEIGHT * _lin(m.days_active, *TRACK_DAYS_SCALE)
                         + (1 - TRACK_DAYS_WEIGHT) * _lin(m.n_closed, *TRACK_TRADES_SCALE))

    if m.return_30d_pct is not None and m.return_prior90d_pct is not None:
        spread = m.return_30d_pct - _monthly_rate(m.return_prior90d_pct, 3)
        c["recent_form"] = _lin(spread, -RECENT_FORM_SPREAD, RECENT_FORM_SPREAD)
    else:
        c["recent_form"] = 50.0 if m.return_30d_pct is not None and m.return_30d_pct >= 0 else 25.0

    if m.avg_leverage is not None:
        c["leverage_discipline"] = (AVG_LEV_WEIGHT * _lin(m.avg_leverage, *AVG_LEV_SCALE)
                                    + (1 - AVG_LEV_WEIGHT) * _lin(m.max_leverage, *MAX_LEV_SCALE))
    else:
        c["leverage_discipline"] = 0.0

    wsum = sum(WEIGHTS.values())
    total = sum(WEIGHTS[k] / wsum * c[k] for k in WEIGHTS)

    red, yellow = [], []
    if m.days_active < RED_MIN_DAYS:
        red.append(f"HISTORY<{RED_MIN_DAYS}d")
    if m.n_closed < RED_MIN_TRADES:
        red.append(f"TRADES<{RED_MIN_TRADES}")
    if m.max_drawdown_pct is not None and m.max_drawdown_pct > RED_MAX_DRAWDOWN_PCT:
        red.append(f"MDD>{RED_MAX_DRAWDOWN_PCT:g}%")
    if pf is not None and pf < RED_MIN_PROFIT_FACTOR:
        red.append(f"PF<{RED_MIN_PROFIT_FACTOR:g}")
    if (m.return_30d_pct is not None and m.return_30d_pct < RED_RECENT_CRASH_PCT
            and (m.return_prior90d_pct or 0) > 0):
        red.append("RECENT_CRASH")

    if m.max_drawdown_pct is None:
        yellow.append("MDD_UNKNOWN")
    if pf is None:
        yellow.append("PF_UNKNOWN")
    if m.days_active < APPROVE_MIN_DAYS:
        yellow.append(f"HISTORY<{APPROVE_MIN_DAYS}d")
    if m.n_closed < APPROVE_MIN_TRADES:
        yellow.append(f"TRADES<{APPROVE_MIN_TRADES}")
    if m.return_30d_pct is not None and m.return_prior90d_pct is not None:
        if m.return_30d_pct < _monthly_rate(m.return_prior90d_pct, 3) - DECAY_SPREAD_PCT:
            yellow.append("DECAY")
    if m.no_stop_loss_pct is not None and m.no_stop_loss_pct > 100 * M.NO_SL_THRESHOLD:
        yellow.append("NO_STOP_LOSS")
    if m.martingale_add_pct is not None and m.martingale_add_pct > 100 * M.MARTINGALE_ADD_THRESHOLD:
        yellow.append("MARTINGALE_ADDS")
    if m.loss_size_escalation_pct is not None and m.loss_size_escalation_pct > 100 * M.SIZE_ESCALATION_THRESHOLD:
        yellow.append("SIZE_UP_AFTER_LOSS")
    if m.top_coin_share_pct is not None and m.top_coin_share_pct > 100 * M.CONCENTRATION_THRESHOLD:
        yellow.append(f"CONCENTRATED:{m.top_coin}")
    if m.max_leverage is not None and m.max_leverage > M.HIGH_MAX_LEVERAGE:
        yellow.append("HIGH_MAX_LEVERAGE")
    if m.sharpe is None:
        yellow.append("SHARPE_N/A")

    approved = (not red
                and m.days_active >= APPROVE_MIN_DAYS
                and m.n_closed >= APPROVE_MIN_TRADES
                and total >= MIN_APPROVAL_SCORE
                and not (set(yellow) & BLOCKING_YELLOW))
    return ScoreResult(round(total, 1), {k: round(v, 1) for k, v in c.items()}, red, yellow, approved)
