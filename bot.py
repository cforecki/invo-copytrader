#!/usr/bin/env python3
"""Invo copy-trading bot: poll followed portfolios, mirror opens/closes.

PAPER by default (logs simulated fills at real Hyperliquid prices; no orders).
Live requires BOTH:  TRADING_MODE=live  in the environment  AND  --live  on the
command line. Either one alone refuses to start.

  python bot.py              # paper, runs forever
  python bot.py --once       # paper, one poll cycle then exit
  python bot.py --status     # print state summary and exit
  python bot.py --reset-breaker
  TRADING_MODE=live python bot.py --live

Watched portfolios = approved_portfolios.json (written by screener.py)
                     + any IDs in WATCHED_PORTFOLIOS (comma-separated).

Exit codes: 0 ok, 2 config error, 3 Invo auth failure (re-auth needed --
supervisors should NOT restart on 3), 4 too many consecutive errors.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

import hl_client
from config import Config, ConfigError
from executors import ExecutionError, make_executor
from invo_client import InvoAuthError, InvoClient, InvoError, InvoRateLimitError
from state import StateStore

log = logging.getLogger("invo.bot")
DISCLAIMER = ("Copy-trading carries substantial risk of loss. Past performance of any trader "
              "does not guarantee future results. This software provides no investment advice.")
MAX_CONSECUTIVE_ERRORS = 10


def inv_key(inv: dict) -> str | None:
    k = inv.get("baseId") or inv.get("id")
    return str(k) if k else None


class MirrorBot:
    def __init__(self, cfg: Config, invo, executor, store: StateStore, watched: list[dict], live: bool,
                 price_fn=None):
        self.cfg = cfg
        self.invo = invo
        self.ex = executor
        self.store = store
        self.watched = watched
        self.live = live
        self.st = store.load("live" if live else "paper", executor.venue if live else f"paper-{cfg.venue}")
        self.st.setdefault("skipped", {})
        self._price_fn = price_fn or self._hl_price
        self._mids, self._mids_at = {}, 0.0

    # ---- prices ---------------------------------------------------------
    def _hl_price(self, coin: str, fallback=None) -> float | None:
        if time.time() - self._mids_at > 10:
            try:
                self._mids, self._mids_at = hl_client.all_mids(), time.time()
            except hl_client.HyperliquidError as e:
                log.warning("HL mids unavailable: %s", e)
        return self._mids.get(coin) or (float(fallback) if fallback else None)

    # ---- startup reconciliation ------------------------------------------
    def reconcile_after_restart(self):
        for bid, pos in list(self.st["positions"].items()):
            status = pos.get("status")
            if status == "open":
                continue
            venue_sz = self.ex.venue_position_size(pos["coin"]) if self.live else None
            if status == "opening":
                if venue_sz:
                    log.warning("RECONCILE %s %s: crashed mid-open but venue shows size %.6f -- adopting "
                                "(entry price unknown; using current price)", bid, pos["coin"], venue_sz)
                    pos.update(status="open", size=venue_sz,
                               entry_price=self._price_fn(pos["coin"]) or pos.get("trader_entry"),
                               adopted_after_crash=True, open_fee=0.0)
                else:
                    log.warning("RECONCILE %s %s: crashed mid-open, no fill -- dropping", bid, pos["coin"])
                    del self.st["positions"][bid]
            elif status == "closing":
                if self.live and venue_sz == 0:
                    log.warning("RECONCILE %s %s: crashed mid-close, venue flat -- marking closed "
                                "(exit price unknown, PnL not recorded; check exchange history)", bid, pos["coin"])
                    self.store.log_fill({"event": "close_unrecorded", "trader_trade_id": bid, **pos,
                                         "ts": time.time()})
                    del self.st["positions"][bid]
                else:
                    pos["status"] = "open"  # close will be retried
        self.store.save(self.st)

    # ---- main cycle -------------------------------------------------------
    def cycle(self):
        st = self.st
        if st["halted"]:
            log.error("HALTED (%s). Not trading. Run with --reset-breaker after reviewing.", st["halt_reason"])
            return
        for p in self.watched:
            pid = p["id"]
            try:
                invs = self.invo.get_open_investments(pid)
            except (InvoAuthError, InvoRateLimitError):
                raise
            except InvoError as e:
                # A failed fetch tells us nothing -- never infer closes from it.
                log.error("fetch %s failed: %s", pid, e)
                continue
            self._process_portfolio(pid, invs)
            if st["halted"]:
                return
        self._check_breaker()
        st["last_cycle_at"] = time.time()
        self.store.save(st)

    def _process_portfolio(self, pid: str, invs: list[dict]):
        st, cfg = self.st, self.cfg
        ps = st["portfolios"].setdefault(pid, {"initialized": False, "preexisting": [], "missing": {}})
        by_id = {k: inv for inv in invs if (k := inv_key(inv))}

        if not ps["initialized"]:
            if not cfg.mirror_existing_on_start:
                ps["preexisting"] = list(by_id)
                if by_id:
                    log.info("%s: %d trades already open at first sight -- not mirroring (late entry)",
                             pid, len(by_id))
            ps["initialized"] = True

        # closes (require the trade to be gone for N consecutive successful polls)
        for bid, pos in list(st["positions"].items()):
            if pos["portfolio_id"] != pid or pos.get("status") != "open":
                continue
            if bid in by_id:
                ps["missing"].pop(bid, None)
                pos["last_ref_price"] = by_id[bid].get("currentPrice")
                continue
            n = ps["missing"][bid] = ps["missing"].get(bid, 0) + 1
            if n >= cfg.close_confirm_polls:
                if self._close(bid, "trader_closed"):
                    ps["missing"].pop(bid, None)

        # opens
        for bid, inv in by_id.items():
            if bid in st["positions"] or bid in ps["preexisting"] or bid in st["skipped"]:
                continue
            self._open(pid, bid, inv)
            if st["halted"]:
                return

        ps["preexisting"] = [b for b in ps["preexisting"] if b in by_id]
        for bid in [b for b, s in st["skipped"].items() if s.get("portfolio_id") == pid and b not in by_id]:
            del st["skipped"][bid]

    def _skip(self, pid, bid, coin, reason):
        log.info("SKIP %s %s (%s): %s", pid[:8], coin, bid[:12], reason)
        self.st["skipped"][bid] = {"portfolio_id": pid, "coin": coin, "reason": reason, "ts": time.time()}

    def _open(self, pid: str, bid: str, inv: dict):
        st, cfg = self.st, self.cfg
        coin = hl_client.normalize_coin(inv.get("ticker", ""))
        is_long = bool(inv.get("directionLong", True))
        open_pos = [p for p in st["positions"].values() if p.get("status") in ("open", "opening", "closing")]

        if cfg.long_only and not is_long:
            return self._skip(pid, bid, coin, "short (LONG_ONLY)")
        if len(open_pos) >= cfg.max_open_positions:
            return self._skip(pid, bid, coin, f"MAX_OPEN_POSITIONS={cfg.max_open_positions}")
        if any(p["coin"] == coin for p in open_pos):
            return self._skip(pid, bid, coin, "coin already held (avoids netting conflicting trades)")

        trader_entry = float(inv.get("entryPrice") or 0)
        px = self._price_fn(coin, inv.get("currentPrice"))
        if not px or not trader_entry:
            return self._skip(pid, bid, coin, "no price")
        drift = abs(px / trader_entry - 1)
        if drift > cfg.max_entry_drift_pct:
            return self._skip(pid, bid, coin, f"price drifted {drift:.2%} from trader entry")

        trader_lev = float(inv.get("leverage") or 1)
        lev = 1.0 if cfg.venue == "binance" else min(trader_lev, cfg.max_leverage)
        equity = self.ex.equity(st)
        margin = min(equity * cfg.trade_allocation_pct, cfg.max_trade_amount_usdt)
        if margin < cfg.min_trade_amount_usdt:
            return self._skip(pid, bid, coin, f"size {margin:.2f} < MIN_TRADE_AMOUNT_USDT")
        notional = margin * lev

        # write-ahead: a crash between here and the fill is reconciled on restart
        st["positions"][bid] = {"status": "opening", "portfolio_id": pid, "coin": coin, "is_long": is_long,
                                "leverage": lev, "trader_leverage": trader_lev, "margin": margin,
                                "trader_entry": trader_entry, "created_at": time.time()}
        self.store.save(st)
        try:
            fill = self.ex.open(coin, is_long, notional, lev, inv.get("currentPrice"))
        except (ExecutionError, Exception) as e:  # noqa: BLE001 -- any venue failure = no position
            del st["positions"][bid]
            self.store.save(st)
            log.error("OPEN FAILED %s %s: %s", coin, bid[:12], e)
            return self._skip(pid, bid, coin, f"execution error: {e}")

        pos = st["positions"][bid]
        pos.update(status="open", size=fill.size, entry_price=fill.price, open_fee=fill.fee,
                   opened_at=fill.ts, order_id=fill.order_id, price_source=fill.price_source)
        st["realized_pnl"] -= fill.fee
        st["n_fills"] += 1
        self.store.log_fill({"event": "open", "mode": "live" if self.live else "paper",
                             "trader_trade_id": bid, "portfolio_id": pid, **fill.to_dict(),
                             "leverage": lev, "margin_usd": margin, "notional_usd": fill.size * fill.price,
                             "trader_entry": trader_entry, "entry_slippage_vs_trader_pct":
                             100 * (fill.price / trader_entry - 1) * (1 if is_long else -1)})
        self.store.save(st)
        log.info("%s OPEN %s %s %.6f @ %.6g lev %.1fx margin $%.2f (trader %s)",
                 "LIVE" if self.live else "PAPER", "LONG" if is_long else "SHORT", coin, fill.size,
                 fill.price, lev, margin, pid[:8])

    def _close(self, bid: str, reason: str) -> bool:
        st = self.st
        pos = st["positions"][bid]
        pos["status"] = "closing"
        self.store.save(st)
        try:
            fill = self.ex.close(pos["coin"], pos["is_long"], pos["size"], pos.get("last_ref_price"))
        except Exception as e:  # noqa: BLE001
            pos["status"] = "open"
            self.store.save(st)
            log.error("CLOSE FAILED %s %s: %s (will retry next cycle)", pos["coin"], bid[:12], e)
            return False
        d = 1 if pos["is_long"] else -1
        gross = d * (fill.price - pos["entry_price"]) * fill.size
        net = gross - fill.fee - pos.get("open_fee", 0.0)
        st["realized_pnl"] += gross - fill.fee
        st["n_fills"] += 1
        self.store.log_fill({"event": "close", "reason": reason, "mode": "live" if self.live else "paper",
                             "trader_trade_id": bid, "portfolio_id": pos["portfolio_id"], **fill.to_dict(),
                             "entry_price": pos["entry_price"], "leverage": pos["leverage"],
                             "margin_usd": pos["margin"], "pnl_usd": round(net, 4),
                             "pnl_pct_of_margin": round(100 * net / pos["margin"], 3) if pos["margin"] else None,
                             "hold_hours": round((fill.ts - pos.get("opened_at", fill.ts)) / 3600, 2)})
        del st["positions"][bid]
        self.store.save(st)
        log.info("%s CLOSE %s %s @ %.6g pnl $%.2f (%s)", "LIVE" if self.live else "PAPER",
                 pos["coin"], "LONG" if pos["is_long"] else "SHORT", fill.price, net, reason)
        return True

    def _check_breaker(self):
        st, cfg = self.st, self.cfg
        eq = self.ex.equity(st)
        if st["starting_equity"] is None:
            st["starting_equity"] = eq
        st["peak_equity"] = max(st["peak_equity"] or eq, eq)
        st["last_equity"] = eq
        loss = 1 - eq / st["starting_equity"] if st["starting_equity"] else 0.0
        log.info("equity $%.2f (start $%.2f, %+.2f%%) | open %d | realized $%.2f",
                 eq, st["starting_equity"], -100 * loss,
                 sum(1 for p in st["positions"].values() if p.get("status") == "open"), st["realized_pnl"])
        if loss >= cfg.circuit_breaker_pct:
            st["halted"] = True
            st["halt_reason"] = (f"circuit breaker: equity ${eq:.2f} is {loss:.1%} below starting "
                                 f"${st['starting_equity']:.2f} (limit {cfg.circuit_breaker_pct:.0%})")
            log.critical(st["halt_reason"])
            if cfg.circuit_breaker_action == "flatten":
                for bid in [b for b, p in st["positions"].items() if p.get("status") == "open"]:
                    self._close(bid, "circuit_breaker")
            self.store.save(st)


def status(st: dict) -> str:
    lines = [f"mode={st['mode']} venue={st['venue']} halted={st['halted']} {st['halt_reason']}",
             f"equity last=${st['last_equity'] or 0:.2f} start=${st['starting_equity'] or 0:.2f} "
             f"realized=${st['realized_pnl']:.2f} fills={st['n_fills']}"]
    for bid, p in st["positions"].items():
        lines.append(f"  {p['status']:8} {p['coin']:8} {'LONG' if p['is_long'] else 'SHORT':5} "
                     f"size={p.get('size', 0):.6g} entry={p.get('entry_price', 0):.6g} lev={p['leverage']}x "
                     f"portfolio={p['portfolio_id'][:8]} trade={bid[:12]}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true", help="enable live trading (also needs TRADING_MODE=live)")
    ap.add_argument("--once", action="store_true", help="run one cycle and exit")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--reset-breaker", action="store_true")
    a = ap.parse_args(argv)

    try:
        cfg = Config.from_env()
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2

    from pathlib import Path
    Path(cfg.log_file).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(cfg.log_file)])

    if a.live != (cfg.trading_mode == "live"):
        print("Refusing to start: live trading needs BOTH TRADING_MODE=live and --live. "
              f"(TRADING_MODE={cfg.trading_mode}, --live={a.live})", file=sys.stderr)
        return 2
    live = a.live

    store = StateStore(cfg.state_file, cfg.fills_log)
    if a.status or a.reset_breaker:
        st = store.load("live" if live else "paper", cfg.venue if live else f"paper-{cfg.venue}")
        if a.reset_breaker:
            st["halted"], st["halt_reason"] = False, ""
            st["starting_equity"] = None  # re-baselined on next cycle
            store.save(st)
            print("Circuit breaker reset; starting equity will be re-baselined next cycle.")
        print(status(st))
        return 0

    watched = cfg.load_watched()
    if not watched:
        print(f"No portfolios to watch. Run screener.py (writes {cfg.watched_portfolios_file}) "
              "or set WATCHED_PORTFOLIOS.", file=sys.stderr)
        return 2
    try:
        cfg.check_rate_budget(len(watched))
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2

    log.warning(DISCLAIMER)
    if live:
        log.warning("=" * 60)
        log.warning("LIVE TRADING on %s. Real orders will be placed. Ctrl-C within 10s to abort.", cfg.venue)
        log.warning("=" * 60)
        time.sleep(10)
    else:
        log.info("PAPER MODE: no orders will be sent. Simulated fills at real Hyperliquid mid prices.")

    try:
        invo = InvoClient()
        executor = make_executor(cfg, live)
        executor.setup()
    except InvoAuthError as e:
        log.critical("%s", e)
        return 3
    except ExecutionError as e:
        log.critical("Executor setup failed: %s", e)
        return 2

    bot = MirrorBot(cfg, invo, executor, store, watched, live)
    bot.reconcile_after_restart()
    log.info("Watching %d portfolios every %ds: %s", len(watched), cfg.poll_interval,
             ", ".join(p.get("name") or p["id"][:8] for p in watched))

    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
    errors = 0
    while not stop["flag"]:
        t0 = time.time()
        try:
            bot.cycle()
            errors = 0
        except InvoAuthError as e:
            log.critical("%s", e)
            return 3
        except InvoRateLimitError as e:
            log.warning("rate-limited, skipping rest of cycle: %s", e)
        except Exception:  # noqa: BLE001 -- keep running, but not forever
            errors += 1
            log.exception("cycle failed (%d/%d consecutive)", errors, MAX_CONSECUTIVE_ERRORS)
            if errors >= MAX_CONSECUTIVE_ERRORS:
                return 4
        if a.once:
            break
        while not stop["flag"] and time.time() - t0 < cfg.poll_interval:
            time.sleep(0.5)
    log.info("stopped; state saved to %s", cfg.state_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
