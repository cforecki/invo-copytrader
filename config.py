"""Bot configuration -- environment variables only (load a .env with `set -a; . ./.env; set +a`).

No secret ever has a default here. Copy .env.example to .env and fill it in;
.env is gitignored.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

INVO_RATE_BUDGET_PER_5MIN = 250


class ConfigError(ValueError):
    pass


def _env(name, default=None, cast=str):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    if cast is bool:
        return v.strip().lower() in ("1", "true", "yes", "on")
    try:
        return cast(v)
    except ValueError as e:
        raise ConfigError(f"{name}={v!r}: {e}") from e


@dataclass
class Config:
    trading_mode: str = "paper"                 # paper | live
    venue: str = "hyperliquid"                  # hyperliquid (perps) | binance (spot)
    trade_allocation_pct: float = 0.05          # margin per trade as fraction of equity
    max_trade_amount_usdt: float = 50.0         # cap on margin per trade
    min_trade_amount_usdt: float = 10.0         # skip trades smaller than this
    max_open_positions: int = 8
    poll_interval: int = 60                     # seconds
    circuit_breaker_pct: float = 0.30           # halt when equity is 30% below starting equity
    circuit_breaker_action: str = "flatten"     # flatten | halt  (flatten = close all, then halt)
    long_only: bool = False
    max_leverage: float = 5.0                   # perps cap; spot always 1x
    max_entry_drift_pct: float = 0.02           # skip if price moved >2% from trader's entry
    max_slippage_pct: float = 0.01              # live market-order slippage bound
    mirror_existing_on_start: bool = False      # don't jump into trades already open when first seen
    close_confirm_polls: int = 2                # a trade must be missing this many polls before we close
    paper_starting_balance: float = 1000.0
    paper_fee_per_side: float = 0.00045
    watched_portfolios_file: str = "approved_portfolios.json"
    watched_portfolios_extra: list[str] = field(default_factory=list)
    state_file: str = "state/bot_state.json"
    fills_log: str = "state/fills.jsonl"
    log_file: str = "state/bot.log"

    @classmethod
    def from_env(cls) -> "Config":
        venue = _env("VENUE", "hyperliquid").lower()
        c = cls(
            trading_mode=_env("TRADING_MODE", "paper").lower(),
            venue=venue,
            trade_allocation_pct=_env("TRADE_ALLOCATION_PCT", 0.05, float),
            max_trade_amount_usdt=_env("MAX_TRADE_AMOUNT_USDT", 50.0, float),
            min_trade_amount_usdt=_env("MIN_TRADE_AMOUNT_USDT", 10.0, float),
            max_open_positions=_env("MAX_OPEN_POSITIONS", 8, int),
            poll_interval=_env("POLL_INTERVAL", 60, int),
            circuit_breaker_pct=_env("CIRCUIT_BREAKER_PCT", 0.30, float),
            circuit_breaker_action=_env("CIRCUIT_BREAKER_ACTION", "flatten").lower(),
            long_only=_env("LONG_ONLY", venue == "binance", bool),
            max_leverage=_env("MAX_LEVERAGE", 5.0, float),
            max_entry_drift_pct=_env("MAX_ENTRY_DRIFT_PCT", 0.02, float),
            max_slippage_pct=_env("MAX_SLIPPAGE_PCT", 0.01, float),
            mirror_existing_on_start=_env("MIRROR_EXISTING_ON_START", False, bool),
            close_confirm_polls=_env("CLOSE_CONFIRM_POLLS", 2, int),
            paper_starting_balance=_env("PAPER_STARTING_BALANCE", 1000.0, float),
            paper_fee_per_side=_env("PAPER_FEE_PER_SIDE", 0.00045, float),
            watched_portfolios_file=_env("WATCHED_PORTFOLIOS_FILE", "approved_portfolios.json"),
            watched_portfolios_extra=[s.strip() for s in _env("WATCHED_PORTFOLIOS", "").split(",") if s.strip()],
            state_file=_env("STATE_FILE", "state/bot_state.json"),
            fills_log=_env("FILLS_LOG", "state/fills.jsonl"),
            log_file=_env("LOG_FILE", "state/bot.log"),
        )
        c.validate()
        return c

    def validate(self) -> None:
        errs = []
        if self.trading_mode not in ("paper", "live"):
            errs.append("TRADING_MODE must be paper or live")
        if self.venue not in ("hyperliquid", "binance"):
            errs.append("VENUE must be hyperliquid or binance")
        if not 0 < self.trade_allocation_pct <= 0.5:
            errs.append("TRADE_ALLOCATION_PCT must be in (0, 0.5]")
        if not 0 < self.min_trade_amount_usdt <= self.max_trade_amount_usdt:
            errs.append("need 0 < MIN_TRADE_AMOUNT_USDT <= MAX_TRADE_AMOUNT_USDT")
        if self.max_open_positions < 1:
            errs.append("MAX_OPEN_POSITIONS must be >= 1")
        if self.poll_interval < 15:
            errs.append("POLL_INTERVAL must be >= 15s")
        if not 0 < self.circuit_breaker_pct < 1:
            errs.append("CIRCUIT_BREAKER_PCT must be in (0, 1) -- 0.30 means halt at a 30% loss")
        if self.circuit_breaker_action not in ("flatten", "halt"):
            errs.append("CIRCUIT_BREAKER_ACTION must be flatten or halt")
        if not 1 <= self.max_leverage <= 50:
            errs.append("MAX_LEVERAGE must be in [1, 50]")
        if self.venue == "binance" and not self.long_only:
            errs.append("VENUE=binance is spot: shorts are impossible, set LONG_ONLY=true")
        if self.close_confirm_polls < 1:
            errs.append("CLOSE_CONFIRM_POLLS must be >= 1")
        if errs:
            raise ConfigError("; ".join(errs))

    def load_watched(self) -> list[dict]:
        """Screener output (approved_portfolios.json) plus WATCHED_PORTFOLIOS extras."""
        out: dict[str, dict] = {}
        p = Path(self.watched_portfolios_file)
        if p.exists():
            data = json.loads(p.read_text())
            for e in data.get("portfolios", []):
                if e.get("enabled", True):
                    out[e["id"]] = {"id": e["id"], "name": e.get("name", ""), "source": "screener",
                                    "score": e.get("score")}
        for pid in self.watched_portfolios_extra:
            out.setdefault(pid, {"id": pid, "name": "", "source": "env", "score": None})
        return list(out.values())

    def check_rate_budget(self, n_portfolios: int) -> None:
        budget = int(os.environ.get("INVO_RATE_LIMIT", "220"))
        per_5min = n_portfolios * (300 / self.poll_interval) + 2  # + token refreshes
        if per_5min > budget:
            raise ConfigError(
                f"{n_portfolios} portfolios every {self.poll_interval}s = ~{per_5min:.0f} Invo calls/5min, "
                f"over INVO_RATE_LIMIT={budget} (Invo hard limit {INVO_RATE_BUDGET_PER_5MIN}/5min/IP). "
                f"Raise POLL_INTERVAL or watch fewer portfolios."
            )
