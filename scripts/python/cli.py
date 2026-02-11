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
from agents.application.crypto_latency_bot import CryptoLatencyBot, CryptoLatencyConfig
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
    min_price: float = 0.88,
    max_price: float = 0.98,
    min_confidence: float = 0.60,
    hours_to_resolution: int = 24,
) -> None:
    """
    Run the resolution scalping bot.

    Uses GTC limit orders (zero maker fees) and price-trust confidence model.

    --dry-run: Simulate trades without executing (default: True)
    --scan-interval: Seconds between scans (default: 120)
    --max-iterations: Max iterations before stopping (default: infinite)
    --simulated-balance: Use simulated balance for testing (e.g., 500)
    --min-price: Min price threshold, e.g., 0.88 = 88% (default: 0.88)
    --max-price: Max price threshold, e.g., 0.98 = 98% (default: 0.98)
    --min-confidence: Min confidence to trade (default: 0.60)
    --hours-to-resolution: Max hours until resolution (default: 24)
    """
    risk_config = RiskConfig()
    scalp_config = ScalpConfig(
        min_price_threshold=min_price,
        max_price_threshold=max_price,
        confidence_threshold=min_confidence,
        hours_to_resolution=hours_to_resolution,
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
    scan_interval: float = 30,
    max_iterations: int = None,
    simulated_balance: float = None,
    min_move: float = 0.15,
    max_entry: float = 0.65,
    asset: str = "btc",
) -> None:
    """
    Run the crypto latency trading bot.

    Exploits price feed latency between Binance and Polymarket's
    15-minute BTC up/down markets. Uses GTC limit orders (zero fees).

    --dry-run: Simulate trades without executing (default: True)
    --scan-interval: Seconds between market scans (default: 30)
    --max-iterations: Max scan iterations before stopping (default: infinite)
    --simulated-balance: Use simulated balance for testing
    --min-move: Min BTC move % to trigger trade (default: 0.15)
    --max-entry: Max Polymarket entry price (default: 0.65)
    --asset: Crypto asset to trade (btc, eth, sol)
    """
    import asyncio

    config = CryptoLatencyConfig(
        min_price_move_pct=min_move,
        max_entry_price=max_entry,
        asset=asset,
    )
    risk_config = RiskConfig()

    bot = CryptoLatencyBot(
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


if __name__ == "__main__":
    app()
