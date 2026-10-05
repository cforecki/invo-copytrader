"""Execution venues. Paper is the default; live executors are only constructed
when bot.py has passed the live gate (TRADING_MODE=live AND --live).

Live dependencies are imported lazily so paper mode needs only `requests`.

Key safety rules enforced here (refuse to start, don't just warn):
  - Hyperliquid: must use an API ("agent") wallet, never the main account
    key. Agent wallets can trade but cannot withdraw. We refuse if the
    signing key's address equals HL_ACCOUNT_ADDRESS.
  - Binance: we query the key's API restrictions and refuse if
    enableWithdrawals is true.
"""

from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import asdict, dataclass

import hl_client

log = logging.getLogger("invo.exec")


class ExecutionError(RuntimeError):
    pass


@dataclass
class Fill:
    coin: str
    side: str          # buy | sell
    size: float        # base units
    price: float
    fee: float         # quote units (USD/USDT)
    ts: float
    venue: str
    order_id: str = ""
    price_source: str = ""

    def to_dict(self):
        return asdict(self)


def _require_env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise ExecutionError(f"{name} is not set (live mode needs it; never hardcode it)")
    return v


# --------------------------------------------------------------------------
class PaperExecutor:
    """Simulated fills at the REAL current Hyperliquid mid price (no slippage
    model beyond the fee). Falls back to Invo's own currentPrice for coins HL
    doesn't list, and records which source was used on every fill."""

    venue = "paper"

    def __init__(self, fee_per_side: float, starting_balance: float):
        self.fee_per_side = fee_per_side
        self.starting_balance = starting_balance
        self._mids: dict[str, float] = {}
        self._mids_at = 0.0

    def mids(self) -> dict[str, float]:
        if time.time() - self._mids_at > 10:
            self._mids = hl_client.all_mids()
            self._mids_at = time.time()
        return self._mids

    def price(self, coin: str, fallback: float | None = None) -> tuple[float, str]:
        try:
            px = self.mids().get(coin)
        except hl_client.HyperliquidError as e:
            log.warning("HL mids unavailable: %s", e)
            px = None
        if px:
            return px, "hyperliquid_mid"
        if fallback:
            return float(fallback), "invo_currentPrice"
        raise ExecutionError(f"no price available for {coin}")

    def setup(self):
        pass

    def open(self, coin, is_long, notional, leverage, ref_price=None) -> Fill:
        px, src = self.price(coin, ref_price)
        size = notional / px
        return Fill(coin, "buy" if is_long else "sell", size, px, notional * self.fee_per_side,
                    time.time(), self.venue, price_source=src)

    def close(self, coin, is_long, size, ref_price=None) -> Fill:
        px, src = self.price(coin, ref_price)
        return Fill(coin, "sell" if is_long else "buy", size, px, size * px * self.fee_per_side,
                    time.time(), self.venue, price_source=src)

    def equity(self, state) -> float:
        unreal = 0.0
        for p in state["positions"].values():
            if p.get("status") != "open":
                continue
            try:
                px, _ = self.price(p["coin"], p.get("last_ref_price"))
            except ExecutionError:
                px = p["entry_price"]
            d = 1 if p["is_long"] else -1
            unreal += d * (px - p["entry_price"]) * p["size"]
        return self.starting_balance + state["realized_pnl"] + unreal

    def venue_position_size(self, coin) -> float | None:
        return None


# --------------------------------------------------------------------------
class HyperliquidExecutor:
    """Fees on fills are ESTIMATED at the base taker rate (the order response
    doesn't report them); account equity from clearinghouseState is the
    authoritative number the circuit breaker uses."""

    venue = "hyperliquid"
    TAKER_FEE_EST = 0.00045

    def __init__(self, max_slippage: float):
        self.max_slippage = max_slippage

    def setup(self):
        try:
            import eth_account
            from hyperliquid.exchange import Exchange
            from hyperliquid.info import Info
            from hyperliquid.utils import constants
        except ImportError as e:
            raise ExecutionError("pip install -r requirements-live.txt (hyperliquid-python-sdk, eth-account)") from e
        key = _require_env("HL_API_WALLET_PRIVATE_KEY")
        self.account = _require_env("HL_ACCOUNT_ADDRESS")
        wallet = eth_account.Account.from_key(key)
        if wallet.address.lower() == self.account.lower():
            raise ExecutionError(
                "HL_API_WALLET_PRIVATE_KEY is your MAIN account key (it can withdraw). Create an API wallet "
                "at app.hyperliquid.xyz -> More -> API, authorize it, and use that key instead."
            )
        self.info = Info(constants.MAINNET_API_URL, skip_ws=True)
        self.exchange = Exchange(wallet, constants.MAINNET_API_URL, account_address=self.account)
        self.sz_decimals = {a["name"]: a["szDecimals"] for a in self.info.meta()["universe"]}
        self.max_lev = {a["name"]: a.get("maxLeverage", 1) for a in self.info.meta()["universe"]}
        log.info("Hyperliquid live executor ready for %s via API wallet %s", self.account, wallet.address)

    def _round_sz(self, coin, sz):
        d = self.sz_decimals[coin]
        return math.floor(sz * 10 ** d) / 10 ** d

    @staticmethod
    def _check(result, what) -> dict:
        if not isinstance(result, dict) or result.get("status") != "ok":
            raise ExecutionError(f"{what} rejected: {result}")
        statuses = result.get("response", {}).get("data", {}).get("statuses", [])
        for s in statuses:
            if isinstance(s, dict) and "error" in s:
                raise ExecutionError(f"{what} rejected: {s['error']}")
            if isinstance(s, dict) and "filled" in s:
                return s["filled"]
        raise ExecutionError(f"{what}: no fill in response {result}")

    def open(self, coin, is_long, notional, leverage, ref_price=None) -> Fill:
        if coin not in self.sz_decimals:
            raise ExecutionError(f"{coin} is not a Hyperliquid perp")
        lev = int(max(1, min(leverage, self.max_lev.get(coin, 1))))
        r = self.exchange.update_leverage(lev, coin, is_cross=False)
        if not isinstance(r, dict) or r.get("status") != "ok":
            raise ExecutionError(f"update_leverage {coin} {lev}x rejected: {r}")
        px = float(self.info.all_mids()[coin])
        sz = self._round_sz(coin, notional / px)
        if sz <= 0:
            raise ExecutionError(f"{coin}: size rounds to 0 for notional {notional:.2f}")
        f = self._check(self.exchange.market_open(coin, is_long, sz, None, self.max_slippage), f"open {coin}")
        filled_sz, avg = float(f["totalSz"]), float(f["avgPx"])
        return Fill(coin, "buy" if is_long else "sell", filled_sz, avg, filled_sz * avg * self.TAKER_FEE_EST,
                    time.time(), self.venue, str(f.get("oid", "")), "exchange_fill")

    def close(self, coin, is_long, size, ref_price=None) -> Fill:
        sz = self._round_sz(coin, size)
        f = self._check(self.exchange.market_close(coin, sz, None, self.max_slippage), f"close {coin}")
        sz_f, avg = float(f["totalSz"]), float(f["avgPx"])
        return Fill(coin, "sell" if is_long else "buy", sz_f, avg, sz_f * avg * self.TAKER_FEE_EST,
                    time.time(), self.venue, str(f.get("oid", "")), "exchange_fill")

    def equity(self, state) -> float:
        st = self.info.user_state(self.account)
        return float(st["marginSummary"]["accountValue"])

    def venue_position_size(self, coin) -> float | None:
        for ap in self.info.user_state(self.account).get("assetPositions", []):
            if ap["position"]["coin"] == coin:
                return abs(float(ap["position"]["szi"]))
        return 0.0


# --------------------------------------------------------------------------
class BinanceSpotExecutor:
    venue = "binance"
    QUOTE = "USDT"

    def setup(self):
        try:
            import ccxt
        except ImportError as e:
            raise ExecutionError("pip install -r requirements-live.txt (ccxt)") from e
        self.ex = ccxt.binance({
            "apiKey": _require_env("BINANCE_API_KEY"),
            "secret": _require_env("BINANCE_API_SECRET"),
            "enableRateLimit": True,
        })
        restr = self.ex.sapi_get_account_apirestrictions()
        if str(restr.get("enableWithdrawals")).lower() == "true":
            raise ExecutionError("This Binance API key has WITHDRAWALS ENABLED. Create a key with "
                                 "withdrawals disabled (and ideally an IP whitelist). Refusing to start.")
        if str(restr.get("enableSpotAndMarginTrading")).lower() != "true":
            raise ExecutionError("Binance API key lacks spot trading permission")
        self.ex.load_markets()
        log.info("Binance spot live executor ready (withdrawals disabled on key: verified)")

    def _symbol(self, coin):
        s = f"{coin}/{self.QUOTE}"
        if s not in self.ex.markets:
            raise ExecutionError(f"{s} not listed on Binance spot")
        return s

    @staticmethod
    def _fee_quote(order, px):
        fee = order.get("fee") or {}
        cost = float(fee.get("cost") or 0)
        return cost if fee.get("currency") in (None, "USDT") else cost * px

    def open(self, coin, is_long, notional, leverage, ref_price=None) -> Fill:
        if not is_long:
            raise ExecutionError("spot cannot short")
        o = self.ex.create_market_buy_order_with_cost(self._symbol(coin), notional)
        o = self.ex.fetch_order(o["id"], self._symbol(coin))
        px = float(o["average"])
        return Fill(coin, "buy", float(o["filled"]), px, self._fee_quote(o, px), time.time(), self.venue,
                    str(o["id"]), "exchange_fill")

    def close(self, coin, is_long, size, ref_price=None) -> Fill:
        sym = self._symbol(coin)
        free = float(self.ex.fetch_balance().get(coin, {}).get("free") or 0)
        qty = float(self.ex.amount_to_precision(sym, min(size, free)))
        o = self.ex.create_market_sell_order(sym, qty)
        o = self.ex.fetch_order(o["id"], sym)
        px = float(o["average"])
        return Fill(coin, "sell", float(o["filled"]), px, self._fee_quote(o, px), time.time(), self.venue,
                    str(o["id"]), "exchange_fill")

    def equity(self, state) -> float:
        bal = self.ex.fetch_balance()
        total = float(bal.get(self.QUOTE, {}).get("total") or 0)
        for p in state["positions"].values():
            if p.get("status") == "open":
                t = self.ex.fetch_ticker(self._symbol(p["coin"]))
                total += p["size"] * float(t["last"])
        return total

    def venue_position_size(self, coin) -> float | None:
        return float(self.ex.fetch_balance().get(coin, {}).get("total") or 0)


def make_executor(cfg, live: bool):
    if not live:
        return PaperExecutor(cfg.paper_fee_per_side, cfg.paper_starting_balance)
    if cfg.venue == "hyperliquid":
        return HyperliquidExecutor(cfg.max_slippage_pct)
    return BinanceSpotExecutor()
