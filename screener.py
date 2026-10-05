#!/usr/bin/env python3
"""Invo trader screener: portfolio IDs in -> ranked CSV + Markdown table out.

Examples:
  python screener.py --ids 6053206f-bd17-4fda-ae27-9cf318aa9a2a https://app.invoapp.com/portfolio/<uuid>
  python screener.py --file portfolios.csv              # columns: id[,label][,hl_address]
  python screener.py --file portfolios.csv --verify-hl  # cross-check on-chain fills where hl_address given
  python screener.py --offline                           # re-score cached raw data after editing scoring.py

Writes:
  reports/screen_<ts>.csv / .md     ranked results
  approved_portfolios.json          the bot's watch list (status APPROVED / APPROVED*)
  data/raw/<id>.json                raw API responses (for offline re-scoring/audit)
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import hl_client
import metrics as M
import scoring
from invo_client import InvoAuthError, InvoClient, InvoError

log = logging.getLogger("invo.screener")
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def parse_targets(ids: list[str], file: str | None) -> list[dict]:
    targets: dict[str, dict] = {}

    def add(raw: str, label: str = "", hl: str = ""):
        mm = UUID_RE.search(raw or "")
        if not mm:
            log.warning("Skipping %r: no portfolio UUID found", raw)
            return
        pid = mm.group(0).lower()
        targets.setdefault(pid, {"id": pid, "label": label, "hl_address": hl})

    for raw in ids or []:
        add(raw)
    if file:
        with open(file, newline="") as fh:
            first = fh.readline()
            fh.seek(0)
            if "," in first and not UUID_RE.search(first.split(",")[0]):
                for row in csv.DictReader(fh):
                    add(row.get("id", ""), row.get("label", "") or "", (row.get("hl_address") or "").strip())
            else:
                for line in fh:
                    line = line.split("#", 1)[0].strip()
                    if line:
                        parts = [p.strip() for p in line.split(",")]
                        add(parts[0], parts[1] if len(parts) > 1 else "", parts[2] if len(parts) > 2 else "")
    return list(targets.values())


def fetch_raw(client: InvoClient, pid: str, max_pages: int) -> dict:
    portfolio = client.get_portfolio(pid)
    open_inv = client.get_open_investments(pid)
    closed_inv, truncated = client.get_all_closed_investments(pid, max_pages=max_pages)
    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "portfolio": portfolio,
        "open": open_inv,
        "closed": closed_inv,
        "truncated": truncated,
    }


def evaluate(target: dict, raw: dict, mids: dict, now: datetime) -> dict:
    p = raw["portfolio"]
    closed = [t for t in (M.trade_from_invo(i) for i in raw["closed"]) if t]
    open_ = [t for t in (M.trade_from_invo(i) for i in raw["open"]) if t]
    dropped = len(raw["closed"]) + len(raw["open"]) - len(closed) - len(open_)
    met = M.compute_metrics(closed, open_, mids, now=now)
    res = scoring.score(met)
    notes = list(met.warnings)
    if raw.get("truncated"):
        notes.append("HISTORY_TRUNCATED(max_pages)")
        res.yellow_flags.append("TRUNCATED")
    if dropped:
        notes.append(f"{dropped} trades unparseable")
    if p.get("liquidated"):
        res.red_flags.append("INVO_LIQUIDATED")
        res.approved = False

    def r(x, nd=1):
        if x is None:
            return ""
        if isinstance(x, float) and math.isinf(x):
            return "inf"
        return round(x, nd)

    owner = (p.get("owner") or {}).get("username") or ""
    return {
        "rank": 0,
        "status": res.status,
        "score": res.score,
        "portfolio_id": target["id"],
        "name": target.get("label") or p.get("title") or "",
        "owner": owner,
        "url": f"https://app.invoapp.com/portfolio/{target['id']}",
        "days_active": r(met.days_active, 0),
        "closed_trades": met.n_closed,
        "open_trades": met.n_open,
        "total_return_pct": r(met.total_return_pct),
        "cagr_pct": r(met.cagr_pct),
        "win_rate_pct": r(met.win_rate_pct),
        "profit_factor": r(met.profit_factor, 2),
        "max_drawdown_pct": r(met.max_drawdown_pct),
        "current_drawdown_pct": r(met.current_drawdown_pct),
        "sharpe": r(met.sharpe, 2),
        "sortino": r(met.sortino, 2),
        "avg_leverage": r(met.avg_leverage),
        "max_leverage": r(met.max_leverage),
        "avg_hold_hours": r(met.avg_hold_hours),
        "trades_per_week": r(met.trades_per_week),
        "profitable_months_pct": r(met.profitable_months_pct),
        "months": met.n_months,
        "return_30d_pct": r(met.return_30d_pct),
        "return_prior90d_pct": r(met.return_prior90d_pct),
        "no_stop_loss_pct": r(met.no_stop_loss_pct),
        "martingale_add_pct": r(met.martingale_add_pct),
        "top_coin": met.top_coin or "",
        "top_coin_share_pct": r(met.top_coin_share_pct),
        "red_flags": " ".join(res.red_flags),
        "yellow_flags": " ".join(res.yellow_flags),
        "invo_win_rate": p.get("winRate", ""),
        "invo_followers": p.get("followerCount", ""),
        "notes": "; ".join(notes),
        **{f"c_{k}": v for k, v in res.components.items()},
        "_approved": res.approved,
    }


MD_COLS = [
    ("rank", "#"), ("status", "Status"), ("score", "Score"), ("name", "Portfolio"), ("owner", "Owner"),
    ("days_active", "Days"), ("closed_trades", "Trades"), ("total_return_pct", "Ret%"), ("cagr_pct", "CAGR%"),
    ("win_rate_pct", "Win%"), ("profit_factor", "PF"), ("max_drawdown_pct", "MDD%"),
    ("current_drawdown_pct", "CurDD%"), ("sharpe", "Sharpe"), ("avg_leverage", "AvgLev"),
    ("max_leverage", "MaxLev"), ("profitable_months_pct", "+Mo%"), ("return_30d_pct", "30d%"),
    ("red_flags", "RED"), ("yellow_flags", "Warnings"),
]


def write_reports(rows: list[dict], out_dir: Path, stamp: str) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"screen_{stamp}.csv"
    md_path = out_dir / f"screen_{stamp}.md"
    fields = [k for k in rows[0] if not k.startswith("_")] if rows else []
    with open(csv_path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    lines = [
        f"# Invo trader screen ({stamp} UTC)",
        "",
        f"> **{scoring.DISCLAIMER}**",
        "",
        "Weights: " + ", ".join(f"{k} {v:.0%}" for k, v in scoring.WEIGHTS.items())
        + f". Approval: no RED, >= {scoring.APPROVE_MIN_DAYS} days, >= {scoring.APPROVE_MIN_TRADES} trades, "
        f"score >= {scoring.MIN_APPROVAL_SCORE:g}.",
        "",
        "| " + " | ".join(h for _, h in MD_COLS) + " |",
        "|" + "---|" * len(MD_COLS),
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(k, "")).replace("|", "/") for k, _ in MD_COLS) + " |")
    lines += [
        "",
        "## Data caveats",
        "- Returns are reconstructed from Invo's per-trade `entrySize` (% of trader balance) x leverage x price "
        f"move, minus an assumed {M.FEE_PER_SIDE:.3%} fee per side. Funding is ignored.",
        "- Max drawdown uses the realized (close-to-close) equity curve plus current open positions; drawdown "
        "*inside* a trade is invisible, so true historical drawdown is likely higher.",
        "- Status `APPROVED*` = approved with warnings; read the Warnings column before following.",
        "- Sharpe/Sortino are annualized from daily realized returns (sqrt(365)) and need "
        f">= {M.MIN_DAYS_FOR_SHARPE} days and >= {M.MIN_TRADES_FOR_SHARPE} trades.",
    ]
    errors = [r for r in rows if r["status"] == "ERROR"]
    if errors:
        lines += ["", "## Errors"] + [f"- `{r['portfolio_id']}`: {r['notes']}" for r in errors]
    md_path.write_text("\n".join(lines) + "\n")
    return csv_path, md_path


def write_approved(rows: list[dict], path: Path, stamp: str) -> list[dict]:
    approved = [
        {"id": r["portfolio_id"], "name": r["name"], "owner": r["owner"], "score": r["score"],
         "status": r["status"], "warnings": r["yellow_flags"], "screened_at": stamp, "enabled": True}
        for r in rows if r.get("_approved")
    ]
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"generated_at": stamp, "disclaimer": scoring.DISCLAIMER,
                               "portfolios": approved}, indent=2))
    os.replace(tmp, path)
    return approved


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ids", nargs="*", default=[], help="portfolio UUIDs or app.invoapp.com/portfolio/<uuid> URLs")
    ap.add_argument("--file", help="text/CSV file of portfolio IDs (optional label, hl_address columns)")
    ap.add_argument("--out-dir", default="reports")
    ap.add_argument("--cache-dir", default="data/raw")
    ap.add_argument("--approved-out", default="approved_portfolios.json")
    ap.add_argument("--max-pages", type=int, default=40, help="closed-trade pages (50/page) per portfolio")
    ap.add_argument("--offline", action="store_true", help="score cached raw data only; no Invo calls")
    ap.add_argument("--verify-hl", action="store_true", help="cross-check rows that have an hl_address")
    ap.add_argument("--no-approved", action="store_true", help="don't overwrite approved_portfolios.json")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    cache = Path(a.cache_dir)
    targets = parse_targets(a.ids, a.file)
    if a.offline and not targets:
        targets = [{"id": p.stem, "label": "", "hl_address": ""} for p in sorted(cache.glob("*.json"))]
    if not targets:
        ap.error("no portfolio IDs given (use --ids and/or --file)")

    client = None
    if not a.offline:
        try:
            client = InvoClient()
        except InvoAuthError as e:
            print(f"\nERROR: {e}", file=sys.stderr)
            return 2
        days = client.refresh_token_days_remaining()
        if days is not None and days < 3:
            log.warning("INVO_REFRESH_TOKEN expires in %.1f days -- re-auth soon", days)

    try:
        mids = hl_client.all_mids()
    except hl_client.HyperliquidError as e:
        log.warning("No Hyperliquid mids (%s); current drawdown excludes open positions", e)
        mids = {}

    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%d_%H%M")
    rows = []
    cache.mkdir(parents=True, exist_ok=True)
    for i, t in enumerate(targets, 1):
        pid = t["id"]
        log.info("[%d/%d] %s", i, len(targets), pid)
        try:
            if a.offline:
                raw = json.loads((cache / f"{pid}.json").read_text())
            else:
                raw = fetch_raw(client, pid, a.max_pages)
                (cache / f"{pid}.json").write_text(json.dumps(raw))
            row = evaluate(t, raw, mids, now)
            if a.verify_hl and t.get("hl_address"):
                try:
                    row.update(hl_client.verify_wallet(t["hl_address"]))
                except hl_client.HyperliquidError as e:
                    row["notes"] = (row["notes"] + f"; HL verify failed: {e}").strip("; ")
            rows.append(row)
        except InvoAuthError as e:
            print(f"\nERROR: {e}", file=sys.stderr)
            return 2  # auth is fatal for the whole run -- never emit a partial "clean" report
        except (InvoError, FileNotFoundError, KeyError, ValueError) as e:
            log.error("%s: %s", pid, e)
            rows.append({"rank": 0, "status": "ERROR", "score": -1, "portfolio_id": pid,
                         "name": t.get("label", ""), "owner": "", "notes": str(e)[:300], "_approved": False})

    order = {"APPROVED": 0, "APPROVED*": 0, "YELLOW": 1, "RED": 2, "ERROR": 3}
    rows.sort(key=lambda r: (order.get(r["status"], 9), -r["score"]))
    for n, r in enumerate(rows, 1):
        r["rank"] = n
    # make CSV columns uniform even when an ERROR row comes first
    keys = list(dict.fromkeys(k for r in rows for k in r))
    rows = [{k: r.get(k, "") for k in keys} for r in rows]

    csv_path, md_path = write_reports(rows, Path(a.out_dir), stamp)
    print(md_path.read_text())
    print(f"CSV: {csv_path}\nMD:  {md_path}")
    if not a.no_approved:
        approved = write_approved(rows, Path(a.approved_out), stamp)
        print(f"Approved for watch list: {len(approved)} -> {a.approved_out}")
    if client:
        print(f"Invo API calls used: {client.calls_made}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
