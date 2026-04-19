import typer
from devtools import pprint

from agents.polymarket.polymarket import Polymarket
from agents.connectors.chroma import PolymarketRAG
from agents.connectors.news import News
from agents.application.trade import Trader
from agents.application.executor import Executor
from agents.application.creator import Creator
from agents.application.arbitrage_trader import DirectionalTrader, DirectionalConfig
from agents.application.resolution_scalper import ResolutionScalper, ScalpConfig
from agents.application.crypto_latency_bot import CryptoLatencyBot, CryptoLatencyConfig, StreakConfig
from agents.application.weather_trader import WeatherTrader, WeatherConfig
from agents.application.negrisk_arb import NegRiskArbBot, NegRiskArbConfig
from agents.application.wallet_monitor import WalletMonitor, WalletMonitorConfig
from agents.application.risk_manager import RiskConfig
from agents.connectors.telegram_alerts import AlertManager, test_alerts

app = typer.Typer()
polymarket = Polymarket()
newsapi_client = News()
polymarket_rag = PolymarketRAG()


@app.command()
def get_all_markets(limit: int = 5, sort_by: str = "spread") -> None:
    """
    Query Polymarket's markets
    """
    print(f"limit: int = {limit}, sort_by: str = {sort_by}")
    markets = polymarket.get_all_markets()
    markets = polymarket.filter_markets_for_trading(markets)
    if sort_by == "spread":
        markets = sorted(markets, key=lambda x: x.spread, reverse=True)
    markets = markets[:limit]
    pprint(markets)


@app.command()
def get_relevant_news(keywords: str) -> None:
    """
    Use NewsAPI to query the internet
    """
    articles = newsapi_client.get_articles_for_cli_keywords(keywords)
    pprint(articles)


@app.command()
def get_all_events(limit: int = 5, sort_by: str = "number_of_markets") -> None:
    """
    Query Polymarket's events
    """
    print(f"limit: int = {limit}, sort_by: str = {sort_by}")
    events = polymarket.get_all_events()
    events = polymarket.filter_events_for_trading(events)
    if sort_by == "number_of_markets":
        events = sorted(events, key=lambda x: len(x.markets), reverse=True)
    events = events[:limit]
    pprint(events)


@app.command()
def create_local_markets_rag(local_directory: str) -> None:
    """
    Create a local markets database for RAG
    """
    polymarket_rag.create_local_markets_rag(local_directory=local_directory)


@app.command()
def query_local_markets_rag(vector_db_directory: str, query: str) -> None:
    """
    RAG over a local database of Polymarket's events
    """
    response = polymarket_rag.query_local_markets_rag(
        local_directory=vector_db_directory, query=query
    )
    pprint(response)


@app.command()
def ask_superforecaster(event_title: str, market_question: str, outcome: str) -> None:
    """
    Ask a superforecaster about a trade
    """
    print(
        f"event: str = {event_title}, question: str = {market_question}, outcome (usually yes or no): str = {outcome}"
    )
    executor = Executor()
    response = executor.get_superforecast(
        event_title=event_title, market_question=market_question, outcome=outcome
    )
    print(f"Response:{response}")


@app.command()
def create_market() -> None:
    """
    Format a request to create a market on Polymarket
    """
    c = Creator()
    market_description = c.one_best_market()
    print(f"market_description: str = {market_description}")


@app.command()
def ask_llm(user_input: str) -> None:
    """
    Ask a question to the LLM and get a response.
    """
    executor = Executor()
    response = executor.get_llm_response(user_input)
    print(f"LLM Response: {response}")


@app.command()
def ask_polymarket_llm(user_input: str) -> None:
    """
    What types of markets do you want trade?
    """
    executor = Executor()
    response = executor.get_polymarket_llm(user_input=user_input)
    print(f"LLM + current markets&events response: {response}")


@app.command()
def run_autonomous_trader() -> None:
    """
    Let an autonomous system trade for you.
    """
    trader = Trader()
    trader.one_best_trade()


@app.command()
def scan_directional() -> None:
    """
    Scan markets for directional trading opportunities.

    Uses LLM superforecaster to find mispriced markets with 15%+ edge.
    """
    trader = DirectionalTrader(dry_run=True)
    opportunities = trader.scan_for_opportunities()
    if opportunities:
        print(f"\nFound {len(opportunities)} directional opportunities:\n")
        for opp in sorted(opportunities, key=lambda x: x.edge, reverse=True):
            print(f"  Market: {opp.question[:60]}...")
            print(f"  Side: {opp.recommended_side} | Market: ${opp.market_price:.2f} | LLM: {opp.llm_probability:.2f} | Edge: {opp.edge:.1%}")
            print()
    else:
        print("No directional opportunities found.")


@app.command()
def run_directional(
    dry_run: bool = True,
    scan_interval: int = 300,
    max_iterations: int = None,
    simulated_balance: float = None,
    min_edge: float = 0.15,
    max_markets: int = 10
) -> None:
    """
    Run the directional trading bot.

    Uses LLM superforecaster to find mispriced markets and trade directionally.

    --dry-run: Simulate trades without executing (default: True)
    --scan-interval: Seconds between scans (default: 300 = 5 min)
    --max-iterations: Max iterations before stopping (default: infinite)
    --simulated-balance: Use simulated balance for testing (e.g., 500 for $500)
    --min-edge: Min edge to trade (default: 0.15 = 15%)
    --max-markets: Max markets to evaluate per scan (default: 10)
    """
    risk_config = RiskConfig()
    directional_config = DirectionalConfig(
        min_edge=min_edge,
        max_markets_to_evaluate=max_markets
    )
    trader = DirectionalTrader(
        risk_config=risk_config,
        directional_config=directional_config,
        dry_run=dry_run,
        simulated_balance=simulated_balance
    )
    trader.run(scan_interval=scan_interval, max_iterations=max_iterations)


@app.command()
def check_balance() -> None:
    """
    Check your wallet's USDC balance on Polygon.
    """
    balance = polymarket.get_usdc_balance()
    address = polymarket.get_address_for_private_key()
    print(f"Wallet Address: {address}")
    print(f"USDC Balance: ${balance:.2f}")


@app.command()
def scan_scalp(limit: int = 100) -> None:
    """
    Scan for resolution scalping opportunities.

    Finds markets where outcome is likely known but price hasn't hit $1.00 yet.
    """
    scalper = ResolutionScalper(dry_run=True)
    opportunities = scalper.scan_for_opportunities()

    if opportunities:
        print(f"\nFound {len(opportunities)} scalping opportunities:\n")
        for opp in sorted(opportunities, key=lambda x: x.potential_profit_percent, reverse=True):
            print(f"  Market: {opp.question[:55]}...")
            print(f"  Side: {opp.recommended_side} @ ${opp.current_price:.2f}")
            print(f"  Profit: {opp.potential_profit_percent:.1%} | Confidence: {opp.confidence_score:.0%}")
            print(f"  Reason: {opp.reason}")
            print()
    else:
        print("No scalping opportunities found.")


@app.command()
def run_scalper(
    dry_run: bool = True,
    scan_interval: int = 120,
    max_iterations: int = None,
    simulated_balance: float = None,
    min_price: float = 0.90,
    max_price: float = 0.98,
    min_confidence: float = 0.60,
) -> None:
    """
    Run the resolution scalping bot (v2 with outcome verification).

    Uses GTC limit orders (zero maker fees), two-tier confidence model,
    and independent outcome verification for crypto/stock markets.

    --dry-run: Simulate trades without executing (default: True)
    --scan-interval: Seconds between scans (default: 120)
    --max-iterations: Max iterations before stopping (default: infinite)
    --simulated-balance: Use simulated balance for testing (e.g., 500)
    --min-price: Min price threshold for verified markets (default: 0.90)
    --max-price: Max price threshold (default: 0.98)
    --min-confidence: Min confidence to trade (default: 0.60)
    """
    risk_config = RiskConfig()
    scalp_config = ScalpConfig(
        min_price_threshold=min_price,
        max_price_threshold=max_price,
        confidence_threshold=min_confidence,
    )

    scalper = ResolutionScalper(
        risk_config=risk_config,
        scalp_config=scalp_config,
        dry_run=dry_run,
        simulated_balance=simulated_balance
    )
    scalper.run(
        scan_interval=scan_interval,
        max_iterations=max_iterations
    )


@app.command()
def redeem() -> None:
    """
    Redeem resolved positions to reclaim USDC.

    Checks for positions that have resolved and claims the USDC payout.
    This is free (only costs gas on Polygon, which is ~$0.01).
    """
    print("Checking for redeemable positions...\n")
    redeemable = polymarket.get_redeemable_positions()

    if not redeemable:
        print("No redeemable positions found.")
        return

    print(f"Found {len(redeemable)} redeemable positions:\n")
    total_value = 0
    for pos in redeemable:
        title = pos.get("title", "Unknown")
        size = float(pos.get("size", 0))
        cur_price = float(pos.get("curPrice", 0))
        value = size * cur_price
        total_value += value
        neg_risk = pos.get("negRisk", False)
        print(f"  {title}")
        print(f"    Shares: {size:.4f} | Value: ~${value:.2f} | Neg-risk: {neg_risk}")
        print()

    print(f"Total estimated redemption: ~${total_value:.2f}\n")

    confirm = typer.confirm("Proceed with redemption?")
    if not confirm:
        print("Cancelled.")
        return

    print("\nRedeeming positions...\n")
    results = polymarket.redeem_positions()

    success = sum(1 for r in results if r["success"])
    failed = sum(1 for r in results if not r["success"])
    print(f"\nResults: {success} redeemed, {failed} failed")

    if success > 0:
        new_balance = polymarket.get_usdc_balance()
        print(f"New USDC balance: ${new_balance:.2f}")


@app.command()
def run_crypto_latency(
    dry_run: bool = True,
    scan_interval: float = 1,
    max_iterations: int = None,
    simulated_balance: float = None,
    min_move: float = 0.30,
    max_entry: float = 0.80,
    assets: str = "btc",
    windows: str = "15",
    initial_wins: int = 0,
    initial_losses: int = 0,
    initial_pnl: float = 0.0,
    streak: bool = False,
    streak_balance: float = 20.0,
    streak_wins: int = 0,
) -> None:
    """
    Run the crypto latency trading bot (fast-path architecture).

    Exploits price feed latency between Binance and Polymarket's
    crypto up/down markets. Uses GTC limit orders (zero fees).

    Fast-path: checks BTC price every 1s, only hits CLOB when signal detected.
    Market cache refreshes every 45s in background.

    --dry-run: Simulate trades without executing (default: True)
    --scan-interval: Seconds between fast-path ticks (default: 1)
    --max-iterations: Max tick iterations before stopping (default: infinite)
    --simulated-balance: Use simulated balance for testing
    --min-move: Min BTC move % to trigger trade (default: 0.30)
    --max-entry: Max Polymarket entry price (default: 0.80)
    --assets: Crypto assets, comma-separated (default: "btc", e.g., "btc,eth,sol,xrp")
    --windows: Market timeframes in minutes, comma-separated (default: "15", e.g., "5,15")
    """
    import asyncio

    market_windows = [int(w.strip()) for w in windows.split(",")]
    asset_list = [a.strip().lower() for a in assets.split(",")]
    config = CryptoLatencyConfig(
        min_price_move_pct=min_move,
        max_entry_price=max_entry,
        assets=asset_list,
        market_windows=market_windows,
    )
    risk_config = RiskConfig()

    streak_config = StreakConfig(enabled=streak, starting_amount=streak_balance) if streak else None
    bot = CryptoLatencyBot(
        config=config,
        risk_config=risk_config,
        dry_run=dry_run,
        simulated_balance=simulated_balance,
        initial_wins=initial_wins,
        initial_losses=initial_losses,
        initial_pnl=initial_pnl,
        streak_config=streak_config,
        initial_streak_wins=streak_wins,
    )

    asyncio.run(bot.run(
        scan_interval=scan_interval,
        max_iterations=max_iterations,
    ))


@app.command()
def run_weather_trader(
    dry_run: bool = True,
    scan_interval: float = None,
    max_iterations: int = None,
    simulated_balance: float = None,
    min_edge: float = 0.08,
    max_entry: float = 0.55,
    market_blend: float = 0.20,
    models: str = "ecmwf_ifs025,gfs_seamless",
    no_observation: bool = False,
    obs_scan_interval: float = None,
) -> None:
    """
    Run the weather temperature trading bot (V3: observation-based).

    Primary strategy: observe actual temperatures from METAR airport stations
    and buy the correct bracket before Polymarket resolves. Falls back to
    ensemble forecasts for markets >8h from resolution.

    No LLM, no paid APIs. Uses free Aviation Weather METAR + Open-Meteo ensembles.

    --dry-run: Simulate trades without executing (default: True)
    --scan-interval: Seconds between scans (default: 600 obs / 1800 forecast)
    --max-iterations: Max scan iterations before stopping (default: infinite)
    --simulated-balance: Use simulated balance for testing
    --min-edge: Min edge for forecast trades (default: 0.08 = 8%)
    --max-entry: Max Polymarket entry price for forecast trades (default: 0.55)
    --market-blend: How much to blend market price into probability (default: 0.20)
    --models: Ensemble models, comma-separated (default: ecmwf_ifs025,gfs_seamless)
    --no-observation: Disable observation mode, forecast-only (default: False)
    --obs-scan-interval: Override observation scan interval in seconds (default: 600)
    """
    import asyncio

    ensemble_models = [m.strip() for m in models.split(",")]
    config_kwargs = dict(
        min_edge=min_edge,
        max_entry_price=max_entry,
        market_blend_weight=market_blend,
        ensemble_models=ensemble_models,
    )
    if no_observation:
        config_kwargs["observation_mode"] = False
    if obs_scan_interval is not None:
        config_kwargs["obs_scan_interval"] = obs_scan_interval

    config = WeatherConfig(**config_kwargs)
    risk_config = RiskConfig()

    bot = WeatherTrader(
        config=config,
        risk_config=risk_config,
        dry_run=dry_run,
        simulated_balance=simulated_balance,
    )

    asyncio.run(bot.run(
        scan_interval=scan_interval,
        max_iterations=max_iterations,
    ))


@app.command()
def run_negrisk_arb(
    dry_run: bool = True,
    scan_interval: float = 30.0,
    max_iterations: int = None,
    simulated_balance: float = None,
    min_spread: float = 0.03,
    max_position: float = 20.0,
    max_days: float = 14.0,
) -> None:
    """
    Run the NegRisk multi-outcome arbitrage scanner.

    Scans multi-outcome negRisk events where sum(YES prices) < $1.00.
    Buys 1 share of every outcome — guaranteed profit at resolution.

    --dry-run: Simulate trades without executing (default: True)
    --scan-interval: Seconds between scans (default: 30)
    --max-iterations: Max scan iterations before stopping (default: infinite)
    --simulated-balance: Use simulated balance for testing
    --min-spread: Min arb spread to trade (default: 0.03 = 3%)
    --max-position: Max position size in USDC (default: 20)
    --max-days: Max days to event resolution (default: 14)
    """
    import asyncio

    config = NegRiskArbConfig(
        min_spread=min_spread,
        max_position_size=max_position,
        max_days_to_resolution=max_days,
    )
    bot = NegRiskArbBot(
        config=config,
        dry_run=dry_run,
        simulated_balance=simulated_balance,
    )
    asyncio.run(bot.run(
        scan_interval=scan_interval,
        max_iterations=max_iterations,
    ))


@app.command()
def run_wallet_monitor(
    dry_run: bool = True,
    poll_interval: float = 30.0,
    max_iterations: int = None,
    simulated_balance: float = None,
    max_copy_size: float = 15.0,
    copy_fraction: float = 0.02,
    min_whale_trade: float = 50.0,
    max_entry_price: float = 0.92,
) -> None:
    """
    Run the Wallet Stalking Strategy.

    Monitors top Polymarket traders and copies their BUY trades.
    Default targets: scottilicious (politics, 86% WR) and winner877 (crypto, 96.6% WR).

    --dry-run: Simulate trades without executing (default: True)
    --poll-interval: Seconds between activity polls (default: 30)
    --max-iterations: Max poll iterations before stopping (default: infinite)
    --simulated-balance: Use simulated balance for testing
    --max-copy-size: Max USDC per copy trade (default: 15)
    --copy-fraction: Fraction of balance per copy (default: 0.02 = 2%)
    --min-whale-trade: Min whale trade USDC to trigger copy (default: 50)
    --max-entry-price: Max entry price to copy (default: 0.92)
    """
    import asyncio

    config = WalletMonitorConfig(
        poll_interval=poll_interval,
        max_copy_size=max_copy_size,
        copy_fraction=copy_fraction,
        min_whale_trade_usdc=min_whale_trade,
        max_entry_price=max_entry_price,
    )

    monitor = WalletMonitor(
        config=config,
        dry_run=dry_run,
        simulated_balance=simulated_balance,
    )

    asyncio.run(monitor.run(
        poll_interval=poll_interval,
        max_iterations=max_iterations,
    ))


@app.command()
def test_telegram() -> None:
    """
    Test Telegram alert integration.

    Sends test messages to verify your bot setup.
    Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env
    """
    import asyncio
    from agents.connectors.telegram_alerts import TelegramAlerter, TradeAlert, PNLUpdate

    async def run_test():
        alerter = TelegramAlerter()

        if not alerter.enabled:
            print("\nTelegram not configured!")
            print("\nTo set up Telegram alerts:")
            print("1. Message @BotFather on Telegram")
            print("2. Send /newbot and follow the instructions")
            print("3. Copy the bot token")
            print("4. Message @userinfobot to get your chat ID")
            print("5. Add to .env:")
            print('   TELEGRAM_BOT_TOKEN="your_token_here"')
            print('   TELEGRAM_CHAT_ID="your_chat_id_here"')
            return

        print("Sending test alerts to Telegram...")

        # Test startup message
        await alerter.send_startup_message("TEST", {
            'balance': 100.0,
            'max_trade_percent': 0.05,
            'max_drawdown': 0.05,
            'scan_interval': 300
        })
        print("  Sent: Startup message")

        # Test trade alert
        await alerter.send_trade_alert(TradeAlert(
            action="BUY",
            market="Will BTC reach $100k by end of 2025?",
            side="YES",
            amount=10.0,
            price=0.45,
            status="DRY_RUN",
            profit_expected=0.50,
        ))
        print("  Sent: Trade alert")

        # Test PNL update
        await alerter.send_pnl_update(PNLUpdate(
            initial_balance=100.0,
            current_balance=105.0,
            total_trades=5,
            winning_trades=4,
            total_profit=5.0,
            return_percent=0.05,
            period="hourly"
        ))
        print("  Sent: PNL update")

        print("\nTest complete! Check your Telegram.")

    asyncio.run(run_test())


@app.command()
def deploy_info() -> None:
    """
    Show deployment instructions for VPS/Docker.
    """
    print("""
================================================================================
POLYMARKET TRADING BOT - DEPLOYMENT OPTIONS
================================================================================

1. VPS DEPLOYMENT (DigitalOcean, AWS, etc.)
-------------------------------------------
   # SSH into your VPS, then run:
   curl -sSL https://raw.githubusercontent.com/goldenAIge/PB-J/main/scripts/bash/deploy.sh | bash

   # Or manually:
   git clone https://github.com/goldenAIge/PB-J.git /opt/polymarket-bot
   cd /opt/polymarket-bot
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env
   nano .env  # Add your API keys

   # Management commands:
   polybot start-directional  # Start directional trader
   polybot start-scalper      # Start resolution scalper
   polybot status             # Check status
   polybot logs               # View logs


2. DOCKER DEPLOYMENT
-------------------------------------------
   # Build and run:
   docker-compose up -d

   # Or single container:
   docker build -t polymarket-bot .
   docker run -d --name polybot --env-file .env polymarket-bot

   # View logs:
   docker-compose logs -f


3. LOCAL DEVELOPMENT
-------------------------------------------
   # Run directional trader:
   python -m agents.application.arbitrage_trader --dry-run

   # Run resolution scalper:
   python -m agents.application.resolution_scalper --dry-run


IMPORTANT NOTES:
- Always start in --dry-run mode first!
- Fund wallet with small amount ($50-100) for testing
- Set up Telegram alerts for monitoring
- Never commit .env file with real keys

================================================================================
""")


@app.command()
def run_directional_v2(
    dry_run: bool = True,
    limit: int = typer.Option(20, help="Number of markets to scan"),
    min_edge: float = typer.Option(0.10, help="Minimum edge to flag as opportunity"),
    interval: int = typer.Option(300, help="Seconds between scans"),
    once: bool = typer.Option(False, help="Run one scan and exit"),
    size: float = typer.Option(25.0, help="USDC size per trade"),
    max_daily_trades: int = typer.Option(3, help="Maximum trades to place per day"),
) -> None:
    """
    Run the Directional V2 strategy (Claude forecaster + predictions logger).

    Scans Polymarket markets using Claude with web search to find mispriced
    opportunities. Logs all predictions for tracking win rate over time.

    --dry-run / --no-dry-run: Safe mode vs live (default: dry-run)
    --limit: Number of markets to scan per cycle (default: 20)
    --min-edge: Minimum edge to flag as opportunity (default: 0.10 = 10%)
    --interval: Seconds between scans (default: 300)
    --once: Run one scan and exit (default: False)
    """
    import os
    import time
    import json as _json
    import httpx
    from datetime import datetime, timezone as _tz
    from pathlib import Path
    from eth_account import Account
    from agents.application.claude_forecaster import ClaudeForecaster
    from agents.application.predictions_logger import PredictionsLogger
    from agents.connectors.telegram_alerts import TelegramAlerter

    FEEDBACK_LOG = Path("research/scanner_feedback.log")
    FEEDBACK_LOG.parent.mkdir(parents=True, exist_ok=True)

    def log_scanner_feedback(**kwargs):
        """Append a JSONL entry to the scanner feedback log."""
        kwargs.setdefault("timestamp", datetime.now(_tz.utc).isoformat())
        with open(FEEDBACK_LOG, "a") as f:
            f.write(_json.dumps(kwargs) + "\n")

    # Derive wallet address for fill verification
    pk = os.getenv("POLYGON_WALLET_PRIVATE_KEY")
    wallet_address = Account.from_key(pk).address.lower() if pk else ""

    mode = "DRY RUN" if dry_run else "LIVE"
    print(f"\n{'='*60}")
    print(f"DIRECTIONAL V2 — {mode}")
    print(f"Min edge: {min_edge:.0%} | Scan limit: {limit} | Interval: {interval}s")
    print(f"Size per trade: ${size} USDC")
    print(f"{'='*60}\n")

    forecaster = ClaudeForecaster(min_edge=min_edge)
    pred_logger = PredictionsLogger()
    alerter = TelegramAlerter()
    trades_placed = 0

    try:
        while True:
            # Sync pending predictions with Polymarket (verify fills, resolve outcomes)
            if wallet_address:
                try:
                    resolved = pred_logger.sync_with_polymarket(wallet_address)
                    for r in resolved:
                        status = r.get("status", "")
                        question = r.get("question", "")[:50]
                        pnl = r.get("pnl") or 0
                        log_scanner_feedback(
                            event="resolved",
                            question=r.get("question", ""),
                            outcome=status,
                            pnl=pnl,
                            size_usdc=r.get("size_usdc", 0),
                        )
                        if status in ("won", "lost"):
                            alerter.send_message_sync(
                                f"{'✅' if status == 'won' else '❌'} POSITION RESOLVED\n"
                                f"Market: {question}\n"
                                f"Result: {status.upper()}\n"
                                f"P/L: ${pnl:.2f}"
                            )
                        elif status == "unfilled":
                            print(f"  ⚠️ Unfilled: {question}")
                except Exception as e:
                    print(f"  ⚠️ Sync failed: {e}")

            opportunities = forecaster.get_opportunities(limit=limit)
            print(f"\nFound {len(opportunities)} opportunities:\n")

            traded_questions = pred_logger.get_all_questions()

            # Also check live Polymarket positions to catch anything not in predictions.json
            active_market_titles = set()
            if wallet_address:
                try:
                    pos_resp = httpx.get(
                        f"https://data-api.polymarket.com/positions?user={wallet_address}",
                        timeout=15,
                    )
                    if pos_resp.status_code == 200:
                        for pos in pos_resp.json():
                            title = pos.get("title") or pos.get("question") or ""
                            if title:
                                active_market_titles.add(title)
                except Exception as e:
                    print(f"  ⚠️ Position check failed: {e}")

            skip_questions = traded_questions | active_market_titles

            for result in opportunities:
                if result.question in skip_questions:
                    print(f"  SKIPPING {result.question[:50]} - already traded this market")
                    continue

                # Confidence-based position sizing
                trade_size = ClaudeForecaster.size_for_confidence(result.confidence_score)
                if trade_size == 0:
                    print(
                        f"  SKIP (low confidence {result.confidence_score}/5): "
                        f"{result.question[:50]}"
                    )
                    continue

                pred_logger.log_prediction(result)
                print(
                    f"  🎯 {result.recommendation} | {result.question[:70]} | "
                    f"price={result.current_price:.2f} | claude={result.claude_probability:.2f} | "
                    f"edge={result.edge:.2f} | confidence={result.confidence_score}/5 | size=${trade_size}"
                )
                execution_result = forecaster.execute_opportunity(result, size_usdc=trade_size, dry_run=dry_run)
                if execution_result["status"] in ("executed", "dry_run"):
                    log_scanner_feedback(
                        event="trade",
                        question=result.question,
                        recommendation=result.recommendation,
                        edge=result.edge,
                        confidence_score=result.confidence_score,
                        reasoning=result.reasoning[:300],
                        size_usdc=trade_size,
                        price=result.current_price,
                        claude_probability=result.claude_probability,
                        dry_run=dry_run,
                    )
                if execution_result["status"] == "executed":
                    print(f"     ✅ Order placed: {execution_result}")
                    alerter.send_message_sync(
                        f"🎯 DIRECTIONAL V2 TRADE\n"
                        f"Action: {result.recommendation}\n"
                        f"Market: {result.question}\n"
                        f"Confidence: {result.confidence_score}/5\n"
                        f"Size: ${trade_size} USDC"
                    )
                    trades_placed += 1
                    if trades_placed >= max_daily_trades:
                        print(f"Daily trade limit of {max_daily_trades} reached. Stopping.")
                        break
                elif execution_result["status"] == "dry_run":
                    print(f"     📋 DRY RUN: size=${trade_size} confidence={result.confidence_score}/5")
                elif execution_result["status"] == "error":
                    print(f"     ❌ Error: {execution_result['error']}")

            if trades_placed >= max_daily_trades:
                break

            stats = pred_logger.get_stats()
            print(f"\n📊 Stats: {stats['total']} total | "
                  f"{stats['won']}W/{stats['lost']}L | "
                  f"{stats['unfilled']} unfilled | {stats['pending']} pending | "
                  f"win rate: {stats['win_rate']:.0%} | "
                  f"P/L: ${stats['total_pnl']:.2f}\n")

            # Daily heartbeat
            alerter.send_message_sync(
                f"🤖 Directional V2 scan complete\n"
                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                f"Markets scanned: {limit}\n"
                f"Opportunities found: {len(opportunities)}\n"
                f"Trades placed: {trades_placed}\n"
                f"Pending: {stats['pending']} | Won: {stats['won']} | Lost: {stats['lost']} | Unfilled: {stats['unfilled']}\n"
                f"Win rate: {stats['win_rate']:.0%}\n"
                f"Total P/L: ${stats['total_pnl']:.2f}"
            )

            if once:
                break

            print(f"Sleeping {interval}s until next scan...")
            time.sleep(interval)

    except KeyboardInterrupt:
        print("\nShutting down directional v2...")


if __name__ == "__main__":
    app()
