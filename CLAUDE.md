# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PB&J is a multi-strategy Polymarket trading bot. Active strategies prioritize zero API cost (no LLM calls). The bot trades on Polygon via Polymarket's CLOB (Central Limit Order Book).

## Commands

```bash
# Run crypto latency bot (primary active strategy)
cd "/Users/pablo/Documents/PB&J"
PYTHONPATH="." python3 scripts/python/cli.py run-crypto-latency --no-dry-run --assets btc,eth,sol --windows 5,15

# Run with streak (compounding) mode
PYTHONPATH="." python3 scripts/python/cli.py run-crypto-latency --no-dry-run --assets btc,eth,sol --windows 5,15 --streak --streak-balance 25 --streak-wins 0

# Startup script (uses venv, checks for existing process)
./scripts/bash/start_bot.sh [streak_balance] [streak_wins]

# Run resolution scalper
PYTHONPATH="." python3 scripts/python/cli.py run-scalper --no-dry-run

# Run wallet monitor (copy-trade strategy)
python -m agents.application.wallet_monitor --live --poll-interval 30
python -m agents.application.wallet_monitor --dry-run  # dry-run mode

# CLI market queries
python3 scripts/python/cli.py get-all-markets
python3 scripts/python/cli.py get-tradeable-markets

# Tests (minimal — most validation is manual dry-run)
pytest tests/

# Formatting
black .
```

## Architecture

### Core API Layer
- `agents/polymarket/polymarket.py` — CLOB client, CTF contract interactions, position redemption, order placement
- `agents/polymarket/gamma.py` — Market data from Gamma API (`get_tradeable_markets()`, `get_weather_markets()`)

### Trading Strategies
- `agents/application/crypto_latency_bot.py` — **Primary active strategy.** Exploits 1-3s price lag between Binance WebSocket and Polymarket 5m/15m crypto up/down markets. Includes streak (compounding) mode
- `agents/application/resolution_scalper.py` — Buys mispriced positions near market resolution (no LLM)
- `agents/application/weather_trader.py` — Basket-based temperature bracket betting using ensemble forecasts (experimental)
- `agents/application/wallet_monitor.py` — **Active.** Wallet stalking copy-trade strategy (scottilicious + winner877). Copies BUY trades, exits when whale exits
- `agents/application/arbitrage_trader.py` — LLM-powered directional trader (paused, costs money)

### Shared Modules
- `agents/application/risk_manager.py` — Portfolio risk: balance caching (30s TTL), position tracking, Kelly sizing, market cooldowns
- `agents/application/outcome_verifier.py` — Free outcome verification via CoinGecko/yfinance/Binance
- `agents/connectors/telegram_alerts.py` — Real-time trade/PNL Telegram notifications
- `scripts/python/cli.py` — Typer CLI with 15+ commands

### Data Flow
Market scan → signal detection → order book check → risk/EV filter → GTC limit order → WebSocket monitoring → resolution verification → auto-redemption

## Critical Patterns

**GTC limit orders everywhere.** Polymarket charges 0% maker fees, 2% taker fees. All strategies use GTC (Good-Til-Canceled) limit orders at best ask to pay zero fees.

**Polygon RPC rate limits.** Default `polygon-rpc.com` returns persistent 401s. Use `polygon-bor-rpc.publicnode.com` (configured via `POLYGON_RPC_URL` env var). Add delays between transactions. ClobClient constructor itself makes RPC calls — minimize instantiation.

**Balance caching.** RiskManager caches balance with 30s TTL to prevent RPC hammering. Multiple strategies share the same cache.

**CTF redemption has two paths:**
- Standard markets: `CTF.redeemPositions(usdcAddr, bytes32(0), conditionId, [1,2])`
- Neg-risk markets: Requires `setApprovalForAll(negRiskAdapter)` first, then `NegRiskAdapter.redeemPositions()`. API field is `negativeRisk` (NOT `negRisk`)

**Logging for background tasks.** Use FileHandler only (no StreamHandler) to avoid pipe-blocking in long-running processes. StreamHandler only for interactive use.

**Market cooldowns.** Markets that fail trade execution get 10-min cooldown. After 2 consecutive non-retryable failures, market is session-blacklisted.

## Environment Variables

Required for trading: `POLYGON_WALLET_PRIVATE_KEY`
Required for alerts: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
Optional: `POLYGON_RPC_URL` (defaults to publicnode), `OPENAI_API_KEY` (only for LLM strategies), `TAVILY_API_KEY`, `NEWSAPI_API_KEY`

Use `load_dotenv('/path/to/.env')` in standalone scripts — `find_dotenv()` breaks with heredoc/stdin execution.

## Common Pitfalls

- Pydantic V2: use `.model_dump()` not `.dict()`
- CLI default `--min-confidence 0.80` can silently override ScalpConfig default of 0.70
- Weather timestamps are LOCAL (Pacific), not UTC
- Python environment: use the project venv at `venv/` or full path `/Library/Frameworks/Python.framework/Versions/3.13/bin/python3`. Set `PYTHONPATH` to project root
- VPN may be required for Polymarket order execution depending on location

## Deployment

- **Local background**: `nohup` + startup script at `scripts/bash/start_bot.sh`
- **Docker**: `docker-compose.yml` with services for scalper, latency, directional, scanner. Profiles: `llm-strategies`, `tools`
- **VPS**: Setup scripts in `scripts/bash/deploy.sh`

## Directional V2 Strategy

Built April 2, 2026. Major rebuild April 4, 2026 (predictions logger rewritten, duplicate prevention fixed, fake P/L eliminated). Uses Claude API with web search to find mispriced Polymarket markets. Cost-optimized via a three-stage pipeline that filters aggressively before expensive API calls.

### Three-Stage Pipeline

1. **MarketPrefilter** (free, Gamma API) — Fetches ~200 markets, filters by volume (>$5K), price range ($0.08–$0.92), resolution date (2–30 days). Markets resolving in <2 days are skipped. Outputs ~15 candidates.
2. **Haiku quick estimate** (~$0.02/market, no web search) — `claude-haiku-4-5` does a fast probability estimate from training knowledge. Markets with `abs(edge) < min_edge` are skipped.
3. **Sonnet + web search** (~$0.30/market) — `claude-sonnet-4-5` with `web_search_20250305` tool. Only called for markets that passed Stage 2.

**Approximate cost per scan: ~$0.50** (most markets filtered at Stage 1–2). DO NOT run large scans (50+ markets) without the pre-filter — costs $10–15 per scan.

### Predictions Logger (rebuilt April 4, 2026)

**WARNING: `predictions.json` was cleared on April 4, 2026. All stats start fresh from this date. Previous entries were dry-run signals logged as if they were real trades — they were not.**

Simple schema with 7 fields only: `market_id`, `question`, `recommendation`, `size_usdc`, `timestamp`, `status`, `pnl`. Status can be: `pending`, `won`, `lost`, `unfilled`.

- **Duplicate prevention**: `get_all_questions()` checks ALL predictions ever logged (pending + won + lost + unfilled). No market is ever re-entered.
- **Fill verification**: `sync_with_polymarket(wallet_address)` queries the Polymarket Data API (`/activity?user=...`) to verify actual fills before marking anything as resolved. P/L is calculated from real fill size and price only.
- **Unfilled tracking**: GTC limit orders that never get matched are marked `unfilled`, excluded from win rate and P/L.

### How to Run

```bash
# Dry run (no orders placed)
PYTHONPATH="." python3 scripts/python/cli.py run-directional-v2 --once --limit 15 --size 25

# Live trading
PYTHONPATH="." python3 scripts/python/cli.py run-directional-v2 --no-dry-run --once --limit 15 --size 25
```

Cron schedule: runs automatically at 8am and 6pm daily. Uses full venv Python path directly (`/Users/pablo/Documents/PB&J/venv/bin/python3`) — no `source activate` needed.

### Key Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `--min-edge` | 0.10 | Minimum edge (10%) to flag as opportunity |
| `--max-daily-trades` | 3 | Maximum trades per day |
| `--size` | 25.0 | USDC per trade |
| `--limit` | 20 | Markets to scan (after pre-filter) |
| `max_days_to_resolution` | 2–30 | Skip markets resolving in <2 days or >30 days |
| `api_delay` | 60 | Seconds between API calls (rate limit) |

### Key Files

| File | Purpose |
|------|---------|
| `agents/connectors/anthropic_client.py` | Claude API client — `quick_estimate()` (Haiku) and `evaluate_market()` (Sonnet + web search) |
| `agents/application/market_prefilter.py` | Free Gamma API pre-filter — volume, price, resolution date |
| `agents/application/claude_forecaster.py` | Orchestrator — two-stage eval, execute_opportunity, scan_markets |
| `agents/application/predictions_logger.py` | Predictions tracker — fill verification, resolution sync, duplicate prevention |
| `agents/data/predictions.json` | Prediction data (cleared April 4, 2026 — fresh start) |
| `scripts/python/cli.py` (`run-directional-v2`) | CLI command with dry-run, trade limits, Telegram alerts |

### Notes

- Telegram alerts fire on executed trades AND resolved positions (won/lost)
- Heartbeat Telegram message sent after every scan with full stats
- Anthropic API rate limit is 30K tokens/min — the 60s delay between calls handles this
- Requires `ANTHROPIC_API_KEY` in `.env`
- Orders use GTC limit (zero maker fees), priced at best ask ± $0.01
- **Auto-redemption is NOT working yet** — winning positions must be manually redeemed on polymarket.com until this is built properly

### Bugs Fixed April 4, 2026

- Bot was entering the same market multiple times across scans (fixed: `get_all_questions()` prevents all duplicates)
- Predictions logger was logging dry-run signals as real trades with fake P/L (fixed: rebuilt logger from scratch, cleared predictions.json)
- P/L calculations were fabricated from edge estimates instead of real fills (fixed: now only verified fills from Data API count)
- Cron was silently failing because `source venv/bin/activate` doesn't work in cron's shell (fixed: uses full venv Python path)
- Duplicate prevention now queries live Polymarket Data API positions in real time (combined with predictions log) so bot never enters a market currently held, even if predictions.json is cleared

### Improvements April 13, 2026

**Confidence-based position sizing.** Stage 2 (Sonnet + web search) now returns `confidence_score` (integer 1-5) alongside the existing probability estimate. Position size scales with confidence:

| Score | Meaning | Trade Size |
|-------|---------|------------|
| 1-2 | Low confidence | Skip (no trade) |
| 3 | Moderate | $15 |
| 4 | High | $25 |
| 5 | Very high | $40 |

The `--size` CLI parameter is no longer used for fixed sizing — `ClaudeForecaster.size_for_confidence()` determines size. Stage 1 (Haiku) prompt is unchanged.

**Feedback logger.** Every trade placement and resolution is logged to `research/scanner_feedback.log` in JSONL format (one JSON object per line). Fields include: event type, question, recommendation, edge, confidence_score, reasoning, size, outcome, pnl. Useful for analyzing which confidence levels and edge thresholds produce the best results.

**Cron schedule updated.** Now runs 3x daily at **6:00 AM, 12:00 PM, and 9:00 PM Pacific** (was 8am and 6pm). Machine must be awake at scan times.

## Wallet Stalker Strategy

Live since April 10, 2026. Monitors top Polymarket traders and copies their trades in real time. Zero API cost (no LLM calls).

### Tracked Wallets

| Wallet | Address | Specialty | Win Rate | Categories Copied |
|--------|---------|-----------|----------|-------------------|
| scottilicious | `0x000d257d2dc7616feaef4ae0f14600fdf50a758e` | Politics | 86% | politics, tech |
| winner877 | `0x85e5669beee6b80d887493e724987dabc5f56056` | Crypto | 96.6% | crypto, sports |

### How It Works

1. **Poll** Data API `/activity` every 30s for each tracked wallet
2. **Detect** new BUY and SELL trades since last poll (5-min staleness filter)
3. **Filter BUYs**: category match, price bounds ($0.05–$0.92), min whale trade $50, order book depth/spread, market cooldown (1hr)
4. **Copy BUY**: GTC limit order at best ask, sized at 2% of balance (max $15)
5. **Track**: record token_id in `agents/data/wallet_monitor_positions.json`
6. **Exit on SELL**: when whale sells a token we hold (matched via tracking file), place GTC limit sell to exit. Only exits positions opened by the wallet monitor — other strategies' positions are never touched
7. **Alert** via Telegram on every copy and exit

### How to Run

```bash
# Live trading
python -m agents.application.wallet_monitor --live --poll-interval 30

# Dry run
python -m agents.application.wallet_monitor --dry-run

# Custom sizing
python -m agents.application.wallet_monitor --live --max-copy-size 15 --copy-fraction 0.02
```

### Key Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `--poll-interval` | 30 | Seconds between activity polls |
| `--max-copy-size` | 15.0 | Max USDC per copy trade |
| `--copy-fraction` | 0.02 | Fraction of balance per copy (2%) |
| `--min-whale-trade` | 50.0 | Ignore whale trades smaller than $50 |
| `--max-entry-price` | 0.92 | Skip tokens priced above this |

### Key Files

| File | Purpose |
|------|---------|
| `agents/application/wallet_monitor.py` | Main strategy: polling, signal evaluation, BUY copy, SELL exit |
| `agents/data/wallet_monitor_positions.json` | Tracks positions opened by this strategy (token_id → metadata) |
| `research/wallet_research.py` | Wallet analysis script |
| `research/wallet_report.txt` | Full wallet analysis and performance data |

### Position Tracking

The wallet monitor maintains its own position tracking file (`wallet_monitor_positions.json`) separate from other strategies. This ensures:
- Exit signals only match positions the wallet monitor opened
- Directional V2, crypto latency, and other strategy positions are never accidentally sold
- New positions are tracked on BUY, removed on SELL exit
- The file is seeded with any pre-existing wallet-monitor positions on first run

### Important Notes

- Category filter uses keyword matching on market title/slug — not perfect but covers major categories
- Market cooldown is 1 hour per market after a copy to avoid duplicate entries
- Max 2 copy trades per poll cycle to avoid overexposure
- Whale SELL trades are matched by token_id against tracked positions only
- VPN required for order execution (Polymarket geoblock)

## Wallet Stalker — Exit Trade Fix (April 13, 2026)

**Problem:** Exit trades were failing with `PolyApiException[status_code=400, error_message={'error': 'not enough balance / allowance'}]` because the CLOB contract (`0xC5d563A36AE78145C45a50134d48A1215220f80a`) was not approved to spend CTF tokens on behalf of the wallet. BUY orders only require USDC (ERC-20) approval, but SELL orders require CTF (ERC-1155) `setApprovalForAll` for the exchange contracts.

**Fix:** Added `ensure_sell_approval()` method to `agents/polymarket/polymarket.py` that checks `isApprovedForAll` on the CTF contract for both exchanges before attempting to sell, and sets approval if not already set. Added `isApprovedForAll` view function to the `erc1155_set_approval` ABI. Uses `Web3.to_checksum_address` on all addresses.

Added `_sell_approval_done` flag to `wallet_monitor.py` so approval is only checked once per session (not on every sell).

Both approvals were set on-chain on April 13, 2026 — permanent, no expiry:
- Exchange (`0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E`): `tx=36a482553862f5ca08cffea670fb430d841081d8e61778fc7112b08fa6d3da73`
- NegRiskExchange (`0xC5d563A36AE78145C45a50134d48A1215220f80a`): `tx=d65845f2fb43ca1fe13d653f3023f22828396c3fd2ae33e3a735eaf34f0d80f2`

The wallet stalker can now successfully execute exit trades when scottilicious or winner877 sells a position.

## 2026-04-19: Pre-V2 Migration Prep

**web3 major version upgrade (6.11.0 → 7.14.1).** The venv was upgraded from web3 v6 to v7 at some point. web3 7.x had breaking changes (middleware API, type handling, contract call patterns). The bots are running fine today, but watch for web3-related issues during V2 migration — specifically pUSD wrapping (`CollateralOnramp.wrap()` calls) and any new V2 contract ABI interactions. If something breaks on-chain, check web3 v7 migration guide first.

**requirements.txt synced** with actual venv state. Old file was a fossil from initial project setup (172 lines, 88 packages not even installed). New file reflects the lean production venv (99 packages). Key version jumps: `py_clob_client` 0.17.5→0.34.6, `web3` 6.11→7.14, `openai` 1.37→2.29, `websockets` 12→15.

## Strategy Research

Prefer backtests, paper/dry-run mode, or small test orders. Avoid advising large live risk without explicit request. Reference `agents/application/backtest.py` for the backtest harness and `docs/STRATEGY_RESEARCH.md` for research notes.
