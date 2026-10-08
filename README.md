# invo-copytrader

A trader **screener** and **copy-trading bot** for [Invo](https://app.invoapp.com) (social copy-trading on
Hyperliquid perps).

- `screener.py` ranks Invo portfolios on a 0–100 risk-weighted score, flags dangerous ones, and writes an
  approved watch list.
- `bot.py` polls the approved portfolios and mirrors their opens and closes. It runs in **paper mode by
  default** and only trades live behind two explicit switches.

Any edge comes from *which* traders you follow and the risk wrapper around them. The app itself doesn't
provide one.

> **Risk notice.** Nothing here is investment advice. Scores summarize past activity reconstructed from Invo
> data, and past performance does not guarantee future results. Copying leveraged perpetuals can lose money
> fast: liquidation, slippage, execution lag behind the source trader, and the trader simply changing
> behaviour all apply. Start in paper mode, and if you go live, use money you can afford to lose.

Patterns are borrowed from [bhevey/invo-mirror-bot](https://github.com/bhevey/invo-mirror-bot) (config
names, Binance spot mirroring) and [plungarini/invo-sentinel](https://github.com/plungarini/invo-sentinel)
(Invo endpoints, refresh-token flow, Hyperliquid order-response checks, leverage caps).

---

## Setup

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt          # screener + paper bot
pip install -r requirements-live.txt     # only if you will ever go live
cp .env.example .env                     # then fill in INVO_REFRESH_TOKEN
set -a; . ./.env; set +a                 # load into the current shell
python -m pytest -q                      # 37 offline tests
```

## 1. Getting your Invo JWT

Invo has no public API. The bot uses the same authenticated API the web app uses, with your own login.

1. Log in at https://app.invoapp.com in a desktop browser.
2. Open DevTools (F12), go to the **Network** tab, and filter by `api.invoapp.com`.
3. Open any portfolio page so requests appear.
4. **Refresh token (long-lived, preferred).** Find a request to `/v1_0/auth/refresh_token`. Logging out
   and back in, or waiting about 10 minutes, forces one. Copy the JWT from its `Authorization: Bearer …`
   request header. Invo doesn't document where it stores this token. If you can't catch the request, check
   **Application → Local Storage → https://app.invoapp.com** for a key that holds a JWT (a string starting
   with `eyJ`).
5. **Access token (optional, about 10 min life).** Copy the `Authorization` header from any other request,
   for example `get_portfolio_by_id`.
6. Save it **once**, using either method:
   - `python invo_auth.py import`, then paste it at the hidden prompt. The tool checks the token with
     Invo before saving it.
   - Put it in `.env` as `INVO_REFRESH_TOKEN=`, or in a cloud environment setting. The `Bearer `
     prefix is optional.

### Automatic refresh and saving

After that one capture, everything is automatic:

- The short-lived access token (about 10 minutes) is refreshed before it expires, and again after any
  401 response.
- **Every refresh is saved** to `~/.config/invo-copytrader/tokens.json`, which you can move with
  `INVO_TOKEN_FILE`.
  - The file is readable only by you (mode 600) and is written atomically. It lives outside the repo
    and is gitignored anyway.
  - If Invo ever issues a new refresh token, the new one is saved too, so a restart never falls back
    to a stale token.
- On startup the client uses the **freshest unexpired** token from the env var and the saved file.
- `python invo_auth.py status` shows expiry dates and where the token came from. It never prints
  token values. `python invo_auth.py refresh` forces a refresh.
- The refresh token lasts about **a year**. The screener and bot warn once it has **less than 14
  days** left, and the bot repeats the check daily.
  - When it finally expires, both tools stop (exit code 2 or 3) and print the re-auth steps. They
    never carry on with empty data.
- To turn saving off, set `INVO_PERSIST_TOKENS=0`.

**What can't be automated: the very first token.** Invo logs in through its web app with Turnkey
(passkey or email code) and has no login API. No reference project has one either; invo-mirror-bot's
"auto re-login" claim has no code behind it. So a person captures the token once, about yearly.

**In Claude Code cloud sessions**, the container is temporary, so the saved file goes with it. Keep
the token in the environment settings as the durable copy. If `invo_auth.py status` ever says the file
holds a newer refresh token than the env var, copy it into the setting.

**Treat these tokens like a password.** Never commit them; `.env` is gitignored.

## 2. Getting portfolio IDs

Open a trader's portfolio in Invo. The ID is the UUID in the URL:

```
https://app.invoapp.com/portfolio/6053206f-bd17-4fda-ae27-9cf318aa9a2a
                                  └──────────── portfolio ID ────────┘
```

You can pass the full URL or just the UUID. For many traders, use a file (see `portfolios.example.csv`; the `label` and `hl_address` columns are optional):

```csv
id,label,hl_address
https://app.invoapp.com/portfolio/<uuid>,Some trader,0xTheirHyperliquidWallet
```

## 3. Running the screener

```sh
python screener.py --ids <uuid-or-url> <uuid-or-url> ...
python screener.py --file portfolios.csv
python screener.py --file portfolios.csv --verify-hl   # cross-check rows that have an hl_address on-chain
python screener.py --offline                            # re-score cached data after editing scoring.py (0 API calls)
```

Outputs:

| File | What |
|---|---|
| `reports/screen_<ts>.md` / `.csv` | Ranked table, every metric, flags, and score components (`c_*` columns) |
| `approved_portfolios.json` | Watch list for the bot (status `APPROVED` / `APPROVED*`) |
| `data/raw/<id>.json` | Raw API responses, for audit and `--offline` re-scoring |

**Metrics per trader:** days active, closed/open trades, total return, CAGR (only at 90 days or more),
win rate, profit factor, max drawdown and current drawdown (including open positions at live HL prices),
Sharpe and Sortino (only with 60+ days and 20+ trades), average and max leverage, average hold time,
trades per week, % profitable months, 30-day return vs. the prior 90 days, % of trades without a stop-loss,
martingale-style adds (averaging down while still in the trade), size-up-after-loss, and top-coin
concentration.

**Score (tune at the top of [`scoring.py`](scoring.py)):** max drawdown 25%, profit factor 20%,
consistency 20%, track record 15%, recent form 10%, leverage discipline 10%. Weights are renormalized, so
you can edit them freely. Missing data scores **0** for that component, because an unknown risk is not a
neutral one.

**Status:**

| Status | Meaning |
|---|---|
| `RED` | Auto-excluded: under 60 days, max drawdown over 40%, profit factor under 1.2, under 30 trades, 30-day return below −20% while the prior 90 days were positive, or the portfolio was liquidated on Invo |
| `YELLOW` | Not RED, but fails approval: under 90 days or 50 trades, score under 60, or a blocking warning (`MDD_UNKNOWN`, `MARTINGALE_ADDS`) |
| `APPROVED` | Passes everything → goes into `approved_portfolios.json` |
| `APPROVED*` | Approved, but with warnings (e.g. `NO_STOP_LOSS`, `DECAY`, `CONCENTRATED:BTC`). Read them. |

**How returns are reconstructed, and what that misses.** Invo gives each trade's entry price, exit price,
leverage, and `entrySize` (margin as a % of the trader's balance). A trade's account return is
`entrySize% × leverage × price move − assumed fees`, and trades are compounded in close order. This has
real blind spots, which are repeated at the bottom of every report:

- **Drawdown inside a trade is invisible.** A trade that went −60% before closing at +5% counts as +5%, so
  the true historical drawdown is likely *higher* than reported.
- Funding is ignored, and fees are an assumption (0.045% per side).
- If `entrySize` is missing, return, drawdown, and Sharpe are left blank rather than guessed, and the
  trader can't be approved.
- History is capped at `--max-pages` × 50 closed trades (default 2,000). Hitting the cap adds a
  `TRUNCATED` flag.
- `--verify-hl` compares Invo's record against the trader's actual Hyperliquid fills (realized PnL, fees,
  win rate over 90 days). It is a sanity check and needs you to know their wallet address.

**Rate limits.** Invo allows 250 requests per 5 minutes per IP. The client throttles itself to
`INVO_RATE_LIMIT` (default 220) and backs off on HTTP 429. A screen costs about `2 + ceil(trades/50)` calls
per portfolio. If you run the screener and the bot at the same time from one IP, split the budget, e.g.
`INVO_RATE_LIMIT=110` each.

## 4. Running the bot in paper mode (default)

```sh
python screener.py --file portfolios.csv     # writes approved_portfolios.json
python bot.py --once                         # one poll cycle, see what it does
python bot.py                                # run forever (Ctrl-C stops cleanly; state is saved)
python bot.py --status                       # equity, open positions, halt status
```

The watch list is `approved_portfolios.json`, plus any IDs in `WATCHED_PORTFOLIOS=` (comma-separated). To
drop a trader, re-run the screener or set `"enabled": false` in the JSON.

Each cycle, every `POLL_INTERVAL` seconds (default 60):

1. Fetch each watched portfolio's open trades.
2. **New trade:** apply the filters, then open. Filters:
   - `LONG_ONLY`
   - `MAX_OPEN_POSITIONS`
   - one position per coin (avoids netting opposite trades on one HL account)
   - the price is within `MAX_ENTRY_DRIFT_PCT` of the trader's entry (no chasing)

   Sizing:
   - margin = `equity × TRADE_ALLOCATION_PCT`, capped at `MAX_TRADE_AMOUNT_USDT`
   - skipped if below `MIN_TRADE_AMOUNT_USDT`
   - leverage = `min(trader's leverage, MAX_LEVERAGE)`

   Trades already open the first time a portfolio is seen are **not** entered late, unless you set
   `MIRROR_EXISTING_ON_START=true`.
3. **Trade gone for `CLOSE_CONFIRM_POLLS` consecutive successful polls:** close it. A *failed* fetch never
   counts as "gone".
4. Mark to market. **Circuit breaker:** if equity is `CIRCUIT_BREAKER_PCT` (default 30%) below starting
   equity, the bot flattens every position (or only halts, with `CIRCUIT_BREAKER_ACTION=halt`) and refuses
   to trade until `python bot.py --reset-breaker`.

Paper fills use the **real current Hyperliquid mid price**, falling back to Invo's `currentPrice` for coins
HL doesn't list. Each fill records which price source it used. Paper has no slippage model beyond fees, so
live results will be worse.

**Files:**

| File | What |
|---|---|
| `state/bot_state.json` | Positions, equity baseline, halt flag. Written atomically after every fill. |
| `state/fills.jsonl` | Append-only log of every open and close: price, size, fee, PnL in USD and as % of margin, hold time, entry slippage vs. the trader |
| `state/bot.log` | Full log |

**Restart safety.** State is written ahead of every order (`opening` / `closing`). After a crash, the bot
reconciles on start:

- paper: a half-open position is dropped, and a half-close is retried;
- live: the bot checks the venue's actual position and adopts it or marks it closed, with a loud log line.

Paper and live use separate state, and the bot refuses to load one's state in the other mode.

**Running under a supervisor.** Exit code 3 means re-authenticate with Invo. Don't let a supervisor
restart-loop on it:

```ini
# /etc/systemd/system/invo-bot.service
[Service]
WorkingDirectory=/opt/invo-copytrader
EnvironmentFile=/opt/invo-copytrader/.env
ExecStart=/opt/invo-copytrader/.venv/bin/python bot.py
Restart=on-failure
RestartSec=30
RestartPreventExitStatus=2 3
```

## 5. Going live

Run paper mode for a few weeks first. Then compare `state/fills.jsonl` against the traders' actual results
to measure your real lag and slippage.

Live needs **both** `TRADING_MODE=live` in the environment **and** `--live` on the command line. Either
one alone refuses to start. On start it prints a banner and waits 10 seconds so you can abort.

**Hyperliquid (perps, default venue).**

1. At app.hyperliquid.xyz go to **More → API**, create an **API wallet**, and authorize it for your
   account. API wallets can trade but **cannot withdraw**.
2. `.env`: set `HL_ACCOUNT_ADDRESS=<your main address>` and
   `HL_API_WALLET_PRIVATE_KEY=<the API wallet's key>`. The bot **refuses to start** if that key belongs to
   your main account address.
3. Positions use isolated margin, at `min(trader leverage, MAX_LEVERAGE, coin's max leverage)`.
4. Run `TRADING_MODE=live python bot.py --live`.

**Binance (spot).** Use `VENUE=binance`, which forces `LONG_ONLY=true` and 1x.

1. Create an API key with **spot trading only and withdrawals disabled**. Add an IP whitelist too.
2. `.env`: set `BINANCE_API_KEY=` and `BINANCE_API_SECRET=`. On start the bot queries the key's
   restrictions and **refuses to run if withdrawals are enabled**.

**Live caveats.**

- The circuit breaker baselines on the account equity at first start, so deposits and withdrawals shift
  it. Run `--reset-breaker` after you move funds.
- Hyperliquid fees on fills are estimated (base taker rate). Account equity is authoritative.
- The bot assumes it is the only thing trading that account.

## 6. Technical analysis (optional): TA-Lib patterns and indicators

[TA-Lib](https://ta-lib.org) 0.8 provides 61 candlestick patterns and about 140 indicators. It runs on
real Hyperliquid candles from the public `candleSnapshot` endpoint, so no keys are needed.

```sh
pip install -r requirements-analysis.txt              # TA-Lib wheel bundles the C library; plus pandas, numpy
python ta_features.py BTC --interval 1h --bars 500    # default indicators + patterns in the last 5 closed bars
python ta_features.py ETH --interval 4h --all --csv eth_4h.csv   # every indicator, full table to CSV
```

- **Default indicators:**
  - RSI14
  - MACD (12/26/9)
  - Bollinger Bands (20, 2σ)
  - ATR14
  - ADX14
  - EMA 20/50/200
  - OBV
- **`--all`:** runs every TA-Lib function in the overlap, momentum, volume, volatility, cycle,
  price-transform and statistic groups with default parameters. MAVP is skipped because it needs a
  per-bar periods series.
- **The still-forming last bar is dropped by default**, because a pattern on an unfinished candle can
  vanish. `--include-forming` keeps it.
- **Pattern values:** +100 is bullish and −100 bearish (some patterns give ±200 for the confirmed
  variant).
  - Indecision/shape patterns (doji, spinning top, high-wave, marubozu, long/short line) are labeled
    as such, not as bullish or bearish. TA-Lib's sign for them is just the candle color.

**Backtest the patterns yourself:**

```sh
python pattern_backtest.py                               # BTC/ETH/SOL 1h, 5000 bars, horizons 4/12/24
```

The backtest enters at the next bar's open, so there is no look-ahead, and charges fees on both sides.
Patterns are chosen on the first half of the data and judged on the second half, which they never saw.
It also reports "excess" return, net of plain trend drift, and a sequential, non-overlapping equity
simulation against buy-and-hold.

**Result of the 2026-10-06 run (Mar–Oct 2026 data):**
- Across all signals, patterns lost money after fees (−4 to −10 bps per trade at 4h/12h).
- Patterns selected on the first half mostly failed on the second half (0/3, 1/6, 1/6 stayed net-positive).
- Every simulated strategy underperformed buy-and-hold.

**Backtest classic strategies, ranked by win rate:**

```sh
python strategy_backtest.py --interval 1h    # also try --interval 4h (about 2.3 years of history)
```

It tests 8 textbook strategies with fixed, un-optimized parameters:
- RSI(14) reversion
- Connors RSI(2)
- Bollinger reversion
- MACD cross
- EMA 20/50 cross
- Donchian breakout
- ADX trend
- a "tight TP / wide SL" trap that demonstrates the win-rate problem

Rules: fill at the next bar's open, stop-loss first when a bar touches both levels, fees on both sides.
Results are reported for each half of the data, next to buy-and-hold.

**Result of the 2026-10-06 run:** the highest win rates were the worst bets.
- The TP 0.5% / SL 5% trap won 89–92% of trades, but one loss (about −430 to −510 bps) erased about
  11 wins.
- Bollinger reversion won 61–69% and lost money in every period.
- The only strategies positive in both halves on 4h were low-win-rate trend followers (ADX+EMA,
  EMA 20/50, Donchian; 31–45% wins). They came with 28–63% drawdowns and large differences between
  coins.

**Trend-following research (wider universe, volatility-scaled, funding included):**

```sh
python trend_research.py                  # 12 coins, daily bars, about 6 years; first run caches funding history
python trend_research.py --interval 4h    # about 2.3 years
```

- Each coin gets an equal sleeve, sized to 40% annual volatility with no leverage.
- Hyperliquid funding is charged hourly from 2023-05 onward, when its data begins.
- Equity is marked to market every bar, so drawdowns include open losses.
- Results are broken down per coin and per year, and a 13-config parameter sweep is shown as a
  distribution, never as a picked winner.

**Result of the 2026-10-08 run:**
- **Daily bars:**
  - Risk-adjusted return was about the same as buy-and-hold (Sharpe 0.8–1.0 vs 0.84–0.88), with much
    smaller drawdowns (38–71% vs 77–84%). The main benefit was largely sidestepping 2022 (−1% to −8%
    vs −74%).
  - All 13 sweep configs were positive, but returns lean heavily on 2021 and on a few coins (DOGE,
    AVAX).
  - Since 2023-05, with full costs, the best headline (ADX+EMA: Sharpe 0.95, max drawdown 23%) matched
    BTC buy-and-hold's Sharpe (0.96) with less than half its drawdown (53%). It did not beat BTC's
    return.
- **4h bars:** weaker than buy-and-hold (Sharpe about 0.46 vs 0.59). EMA and Donchian lost money in
  2025.

> **Analysis only.** None of this feeds `bot.py`'s decisions. Candlestick patterns have weak
> out-of-sample evidence, and none of these signals has been validated for this strategy. Using one as
> an entry/exit filter would need its own backtest first. Past behavior of a pattern or indicator does
> not guarantee future results.

## Project layout

| File | Role |
|---|---|
| `screener.py` | CLI: fetch, compute metrics, score, write CSV/MD reports and `approved_portfolios.json` |
| `scoring.py` | **Weights and thresholds at the top**, score, and RED/YELLOW flags |
| `metrics.py` | Trade normalization and every metric, with the reconstruction assumptions documented |
| `invo_client.py` | Invo API: auth/refresh, rate limiting, 429 backoff, pagination, loud failures |
| `hl_client.py` | Hyperliquid public API: mids, fills, account state (verification) |
| `bot.py` | Poll loop, open/close detection, sizing, circuit breaker, restart reconciliation |
| `executors.py` | Paper, Hyperliquid (API wallet), and Binance spot (withdraw check) executors |
| `config.py` | Env-var config with validation and a rate-budget check |
| `state.py` | Atomic JSON state and fills log |
| `ratelimit.py` | Sliding-window limiter |
| `token_store.py` / `invo_auth.py` | Saved Invo tokens (0600, atomic) and the import/status/refresh CLI |
| `pattern_backtest.py` | Optional: in-sample/out-of-sample backtest of candlestick patterns on real HL candles |
| `strategy_backtest.py` | Optional: classic indicator strategies backtested on real HL candles, ranked by win rate with expectancy/OOS beside it |
| `trend_research.py` | Optional: multi-coin trend-following research with vol sizing, funding, per-year/per-coin robustness |
| `ta_features.py` | Optional: TA-Lib candlestick patterns + indicators on real HL candles (analysis only) |

## What is verified vs. assumed

Verified while building this:

- The Invo base URL and `GET /v1_0/auth/refresh_token` respond as expected: 401 without auth, and a bogus
  refresh token gets a clean rejection.
- Hyperliquid's `/info` mids work.
- `hyperliquid-python-sdk` 0.24 and `ccxt` 4.5 method signatures match the calls in `executors.py`.

Not verified, because it needs an Invo login or funds:

- **Screener:** the exact shape of authenticated Invo responses. Field names follow invo-sentinel and
  invo-mirror-bot, which observed them live.
- **Bot:** any live order placement.

Run the screener on a couple of portfolios you know and sanity-check the numbers against the Invo app
before relying on it.
