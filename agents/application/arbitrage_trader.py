"""
Directional Trader for Polymarket

Strategy: LLM-powered directional trading on mispriced markets
- Scan liquid, mid-range markets (price 0.15-0.85)
- Use LLM superforecaster to estimate true probability
- Trade when LLM probability diverges from market price by 15%+
- Kelly-based position sizing for optimal capital allocation
- Fewer, larger, higher-conviction trades

Replaces the old arbitrage strategy (YES+NO < 0.98) which had 0/3 success
rate because opportunities don't exist on efficient markets.

Usage:
    python -m agents.application.arbitrage_trader [--dry-run] [--scan-interval 300]
"""

import os
import sys
import time
import json
import logging
import argparse
import re
from datetime import datetime
from typing import Optional
from dataclasses import dataclass, asdict

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from agents.polymarket.polymarket import Polymarket
from agents.polymarket.gamma import GammaMarketClient
from agents.application.risk_manager import RiskConfig, PortfolioRiskManager
from agents.application.executor import Executor
from agents.connectors.telegram_alerts import TelegramAlerter, TradeAlert

load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('trades.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


class DirectionalConfig(BaseModel):
    """Configuration for directional trading"""
    min_edge: float = Field(default=0.15, description="Min edge to trade (15%)")
    min_price: float = Field(default=0.15, description="Don't buy below 15 cents")
    max_price: float = Field(default=0.85, description="Don't buy above 85 cents")
    max_markets_to_evaluate: int = Field(default=10, description="Max LLM calls per scan")
    max_spread: float = Field(default=0.04, description="Max order book spread")


@dataclass
class DirectionalOpportunity:
    """Represents a directional trading opportunity"""
    market_id: str
    question: str
    recommended_side: str  # "YES" or "NO"
    market_price: float
    llm_probability: float
    edge: float
    token_id: str
    volume_24h: float
    liquidity: float
    timestamp: str


@dataclass
class TradeRecord:
    """Record of an executed trade"""
    timestamp: str
    market_id: str
    question: str
    side: str
    amount: float
    entry_price: float
    llm_probability: float
    edge: float
    expected_profit: float
    status: str
    tx_hash: Optional[str] = None


class DirectionalTrader:
    """
    LLM-powered directional trading bot for Polymarket.

    Finds mispriced markets where LLM superforecaster disagrees
    with the market by 15%+ and takes a directional position.
    """

    def __init__(
        self,
        risk_config: Optional[RiskConfig] = None,
        directional_config: Optional[DirectionalConfig] = None,
        dry_run: bool = True,
        simulated_balance: Optional[float] = None
    ):
        self.polymarket = Polymarket()
        self.gamma = GammaMarketClient()
        self.executor = Executor()
        self.risk_config = risk_config or RiskConfig()
        self.directional_config = directional_config or DirectionalConfig()
        self.dry_run = dry_run
        self.simulated_balance = simulated_balance

        # Shared risk manager
        self.risk_manager = PortfolioRiskManager(self.polymarket, self.risk_config)

        # Telegram alerts
        self.telegram = TelegramAlerter()

        # Trading state
        self.initial_balance: Optional[float] = None
        self.current_balance: float = 0.0
        self.total_trades: int = 0
        self.successful_trades: int = 0
        self.trade_history: list[TradeRecord] = []

        # Already evaluated markets this session
        self.evaluated_markets: set[str] = set()

        logger.info(f"DirectionalTrader initialized (dry_run={dry_run})")
        if simulated_balance:
            logger.info(f"Using SIMULATED balance: ${simulated_balance:.2f}")
        logger.info(f"Config: min_edge={self.directional_config.min_edge}, price_range=[{self.directional_config.min_price}, {self.directional_config.max_price}]")

    def get_wallet_balance(self) -> float:
        """Get current USDC balance"""
        if self.simulated_balance is not None:
            return self.simulated_balance
        return self.risk_manager.get_balance()

    def initialize_balance(self) -> bool:
        """Initialize starting balance"""
        self.current_balance = self.get_wallet_balance()
        if self.initial_balance is None:
            self.initial_balance = self.current_balance

        logger.info(f"Wallet balance: ${self.current_balance:.2f}")

        if self.current_balance < self.risk_config.min_trade_size:
            logger.warning(f"Balance too low (min: ${self.risk_config.min_trade_size})")
            return False
        return True

    def scan_for_opportunities(self) -> list[DirectionalOpportunity]:
        """Scan liquid markets for directional trading opportunities."""
        opportunities = []

        try:
            logger.info("Scanning for directional trading opportunities...")

            # Fetch top liquid markets
            markets = self.gamma.get_tradeable_markets(limit=50, order_by="volume24hr")
            logger.info(f"Fetched {len(markets)} liquid markets")

            # Pre-filter for price range, volume, liquidity
            candidates = []
            for market in markets:
                candidate = self._prefilter_market(market)
                if candidate:
                    candidates.append(candidate)

            logger.info(f"Pre-filtered to {len(candidates)} candidates in price range [{self.directional_config.min_price}, {self.directional_config.max_price}]")

            # Run LLM superforecaster on top candidates (limited by max_markets_to_evaluate)
            llm_evaluated = 0
            for market_data in candidates[:self.directional_config.max_markets_to_evaluate]:
                if llm_evaluated >= self.directional_config.max_markets_to_evaluate:
                    break

                market_id = str(market_data['id'])

                # Skip already evaluated or on cooldown
                if market_id in self.evaluated_markets:
                    continue
                if self.risk_manager.is_market_on_cooldown(market_id):
                    continue

                opp = self._evaluate_with_llm(market_data)
                llm_evaluated += 1
                self.evaluated_markets.add(market_id)

                if opp:
                    opportunities.append(opp)

            if opportunities:
                # Sort by edge (highest edge first)
                opportunities.sort(key=lambda x: x.edge, reverse=True)
                logger.info(f"Found {len(opportunities)} opportunities with sufficient edge!")
                for opp in opportunities:
                    logger.info(f"  - {opp.question[:50]}...")
                    logger.info(f"    Side: {opp.recommended_side} | Market: {opp.market_price:.2f} | LLM: {opp.llm_probability:.2f} | Edge: {opp.edge:.1%}")
            else:
                logger.info(f"No opportunities found (evaluated {llm_evaluated} markets)")

        except Exception as e:
            logger.error(f"Error scanning markets: {e}")

        return opportunities

    def _prefilter_market(self, market: dict) -> Optional[dict]:
        """Pre-filter a market before LLM evaluation."""
        try:
            question = market.get('question', '')
            if not question:
                return None

            # Volume/liquidity filters
            volume_24h = float(market.get('volume24hr', 0) or 0)
            liquidity = float(market.get('liquidityClob', 0) or market.get('liquidity', 0) or 0)

            if volume_24h < self.risk_config.min_volume_24h:
                return None
            if liquidity < self.risk_config.min_liquidity_usd:
                return None

            if not market.get('enableOrderBook', False):
                return None
            if not market.get('acceptingOrders', True):
                return None

            # Parse prices
            outcome_prices = market.get('outcomePrices', '[]')
            if isinstance(outcome_prices, str):
                outcome_prices = json.loads(outcome_prices)

            if len(outcome_prices) != 2:
                return None

            yes_price = float(outcome_prices[0])
            no_price = float(outcome_prices[1])

            clob_token_ids = market.get('clobTokenIds', '[]')
            if isinstance(clob_token_ids, str):
                clob_token_ids = json.loads(clob_token_ids)

            if len(clob_token_ids) != 2:
                return None

            # Check if at least one side is in mid-range
            yes_in_range = self.directional_config.min_price <= yes_price <= self.directional_config.max_price
            no_in_range = self.directional_config.min_price <= no_price <= self.directional_config.max_price

            if not yes_in_range and not no_in_range:
                return None

            return {
                'id': market.get('id', ''),
                'question': question,
                'description': market.get('description', ''),
                'yes_price': yes_price,
                'no_price': no_price,
                'yes_token_id': clob_token_ids[0],
                'no_token_id': clob_token_ids[1],
                'volume_24h': volume_24h,
                'liquidity': liquidity,
            }

        except Exception as e:
            logger.debug(f"Prefilter error: {e}")
            return None

    def _evaluate_with_llm(self, market_data: dict) -> Optional[DirectionalOpportunity]:
        """Evaluate a market using LLM superforecaster."""
        question = market_data['question']
        description = market_data.get('description', '')
        yes_price = market_data['yes_price']
        no_price = market_data['no_price']

        logger.info(f"Evaluating: {question[:60]}... (YES={yes_price:.2f}, NO={no_price:.2f})")

        try:
            # Get LLM probability for YES outcome
            response = self.executor.get_superforecast(
                event_title=description or question,
                market_question=question,
                outcome="Yes"
            )

            llm_yes_prob = self._parse_probability(response)
            if llm_yes_prob is None:
                logger.debug(f"Could not parse LLM probability from response")
                return None

            llm_no_prob = 1.0 - llm_yes_prob

            # Calculate edge for each side
            yes_edge = llm_yes_prob - yes_price  # Positive = YES is underpriced
            no_edge = llm_no_prob - no_price  # Positive = NO is underpriced

            logger.info(f"  LLM: YES={llm_yes_prob:.2f} (edge={yes_edge:+.2f}), NO={llm_no_prob:.2f} (edge={no_edge:+.2f})")

            # Find the best side
            if yes_edge >= no_edge and yes_edge >= self.directional_config.min_edge:
                side = "YES"
                price = yes_price
                edge = yes_edge
                token_id = market_data['yes_token_id']
                llm_prob = llm_yes_prob
            elif no_edge > yes_edge and no_edge >= self.directional_config.min_edge:
                side = "NO"
                price = no_price
                edge = no_edge
                token_id = market_data['no_token_id']
                llm_prob = llm_no_prob
            else:
                logger.info(f"  Edge too small (min {self.directional_config.min_edge:.0%}), skipping")
                return None

            # Check price is in range
            if price < self.directional_config.min_price or price > self.directional_config.max_price:
                return None

            return DirectionalOpportunity(
                market_id=str(market_data['id']),
                question=question,
                recommended_side=side,
                market_price=price,
                llm_probability=llm_prob,
                edge=edge,
                token_id=token_id,
                volume_24h=market_data['volume_24h'],
                liquidity=market_data['liquidity'],
                timestamp=datetime.now().isoformat()
            )

        except Exception as e:
            logger.error(f"LLM evaluation failed for {question[:40]}: {e}")
            return None

    def _parse_probability(self, response: str) -> Optional[float]:
        """Parse probability from LLM response."""
        # Try structured format first: PROBABILITY: 0.XX
        match = re.search(r'PROBABILITY:\s*(0\.\d+|1\.00?)', response)
        if match:
            return float(match.group(1))

        # Fallback: find likely probability values
        matches = re.findall(r'(\d\.\d+)', response)
        for m in matches:
            val = float(m)
            if 0.01 <= val <= 0.99:
                return val

        return None

    def execute_trade(self, opportunity: DirectionalOpportunity) -> Optional[TradeRecord]:
        """Execute a directional trade with full risk checks."""
        market_id = opportunity.market_id

        # Risk checks
        if self.risk_manager.is_market_on_cooldown(market_id):
            logger.info(f"Market {market_id} on cooldown, skipping")
            return None

        # Kelly-based position sizing
        trade_size = self.risk_manager.calculate_position_size(
            opportunity.edge, opportunity.market_price
        )

        if trade_size < self.risk_config.min_trade_size:
            logger.warning(f"Position size ${trade_size} too small (min ${self.risk_config.min_trade_size})")
            return None

        # Check risk limits
        can_trade, reason = self.risk_manager.can_open_position(trade_size)
        if not can_trade:
            logger.warning(f"Risk check failed: {reason}")
            return None

        # Order book verification
        ob_info = self.risk_manager.get_order_book_depth(
            opportunity.token_id, "BUY", opportunity.market_price
        )

        if ob_info is None:
            logger.warning("Cannot read order book, skipping")
            self.risk_manager.record_failed_market(market_id)
            return None

        if ob_info.spread > self.directional_config.max_spread:
            logger.warning(f"Spread too wide: ${ob_info.spread:.4f}")
            return None

        # Check depth
        shares_needed = trade_size / opportunity.market_price
        if ob_info.can_fill_amount < shares_needed * 0.5:
            logger.warning(f"Insufficient depth for ${trade_size:.2f} trade")
            return None

        # Calculate expected profit
        shares = trade_size / opportunity.market_price
        expected_value = shares * opportunity.llm_probability
        expected_profit = expected_value - trade_size

        trade = TradeRecord(
            timestamp=datetime.now().isoformat(),
            market_id=market_id,
            question=opportunity.question,
            side=opportunity.recommended_side,
            amount=trade_size,
            entry_price=opportunity.market_price,
            llm_probability=opportunity.llm_probability,
            edge=opportunity.edge,
            expected_profit=expected_profit,
            status="PENDING"
        )

        logger.info(f"\n{'='*60}")
        logger.info("EXECUTING DIRECTIONAL TRADE")
        logger.info(f"Market: {opportunity.question[:60]}...")
        logger.info(f"Side: {opportunity.recommended_side} @ ${opportunity.market_price:.4f}")
        logger.info(f"LLM Prob: {opportunity.llm_probability:.2f} | Edge: {opportunity.edge:.1%}")
        logger.info(f"Order book: spread=${ob_info.spread:.4f}, depth={ob_info.depth:.0f}")
        logger.info(f"Trade size: ${trade_size:.2f} (Kelly) | Expected profit: ${expected_profit:.2f}")
        logger.info(f"{'='*60}\n")

        if self.dry_run:
            logger.info("[DRY RUN] Trade simulated")
            trade.status = "DRY_RUN"

            if self.simulated_balance is not None:
                self.simulated_balance -= trade_size
                # Simulate: if LLM is right, profit = shares * $1 - trade_size
                # We don't know yet, so just log the trade
                logger.info(f"[DRY RUN] Deployed: ${trade_size:.2f} | Expected EV: ${expected_profit:+.2f}")

            self.successful_trades += 1
        else:
            try:
                logger.warning(">>> EXECUTING REAL TRADE <<<")

                # Use limit order for better pricing
                order_response = self.polymarket.execute_order(
                    price=opportunity.market_price,
                    size=shares,
                    side="BUY",
                    token_id=opportunity.token_id
                )

                logger.info(f"Order submitted: {order_response}")
                trade.status = "SUBMITTED"
                trade.tx_hash = str(order_response) if order_response else "no_hash"
                self.successful_trades += 1

            except Exception as e:
                error_msg = str(e).lower()
                if "balance" in error_msg or "insufficient" in error_msg:
                    self.risk_manager.get_balance(force_refresh=True)
                    self.risk_manager.record_failed_market(market_id)

                logger.error(f"Trade execution failed: {e}")
                trade.status = "FAILED"
                self.risk_manager.record_failed_market(market_id)

        self.total_trades += 1
        self.trade_history.append(trade)
        self.log_trade(trade)

        # Telegram alert
        try:
            self.telegram.send_message_sync(
                f"{'[DRY]' if self.dry_run else ''} <b>DIRECTIONAL {trade.status}</b>\n\n"
                f"{opportunity.question[:50]}...\n"
                f"BUY {opportunity.recommended_side} @ ${opportunity.market_price:.4f}\n"
                f"LLM Probability: {opportunity.llm_probability:.0%}\n"
                f"Edge: {opportunity.edge:.1%}\n"
                f"Size: ${trade_size:.2f}\n"
                f"Expected EV: ${expected_profit:+.2f}\n"
            )
        except Exception as e:
            logger.warning(f"Failed to send Telegram alert: {e}")

        return trade

    def log_trade(self, trade: TradeRecord):
        """Log trade to file"""
        try:
            with open('trades.log', 'a') as f:
                f.write(json.dumps(asdict(trade)) + '\n')
        except Exception as e:
            logger.error(f"Failed to log trade: {e}")

    def print_status(self):
        """Print current trading status"""
        logger.info(f"\n{'='*40}")
        logger.info("DIRECTIONAL TRADING STATUS")
        logger.info(f"{'='*40}")
        logger.info(f"Balance: ${self.current_balance:.2f}")
        if self.initial_balance:
            profit = self.current_balance - self.initial_balance
            logger.info(f"Initial: ${self.initial_balance:.2f}")
            logger.info(f"P&L: ${profit:+.2f}")
        logger.info(f"Total trades: {self.total_trades}")
        logger.info(f"Successful: {self.successful_trades}")
        logger.info(f"Markets evaluated: {len(self.evaluated_markets)}")
        logger.info(f"Dry run mode: {self.dry_run}")
        logger.info(f"{'='*40}\n")

    def run(self, scan_interval: int = 300, max_iterations: Optional[int] = None):
        """Main trading loop"""
        logger.info(f"\n{'='*60}")
        logger.info("STARTING DIRECTIONAL TRADER")
        logger.info(f"Scan interval: {scan_interval}s")
        logger.info(f"Mode: {'DRY RUN' if self.dry_run else 'LIVE TRADING'}")
        logger.info(f"{'='*60}\n")

        if not self.dry_run:
            logger.warning("=" * 60)
            logger.warning("WARNING: LIVE TRADING MODE - REAL FUNDS AT RISK")
            logger.warning("=" * 60)
            time.sleep(5)

        iteration = 0

        try:
            while max_iterations is None or iteration < max_iterations:
                iteration += 1
                logger.info(f"\n--- Iteration {iteration} ---")

                # Reset evaluated markets each scan (prices change)
                self.evaluated_markets.clear()

                if not self.initialize_balance():
                    logger.error("Insufficient balance. Stopping.")
                    break

                # Scan for opportunities
                opportunities = self.scan_for_opportunities()

                # Execute best opportunity
                if opportunities:
                    best = opportunities[0]
                    self.execute_trade(best)

                self.print_status()

                logger.info(f"Waiting {scan_interval}s until next scan...")
                time.sleep(scan_interval)

        except KeyboardInterrupt:
            logger.info("\nTrader stopped by user")
        except Exception as e:
            logger.error(f"Trading loop error: {e}")
        finally:
            self.print_status()
            logger.info("Directional trader stopped")


def main():
    parser = argparse.ArgumentParser(description='Polymarket Directional Trader')
    parser.add_argument('--dry-run', action='store_true', default=True,
                        help='Run in dry-run mode (no real trades)')
    parser.add_argument('--live', action='store_true',
                        help='Run in live mode (REAL TRADES)')
    parser.add_argument('--scan-interval', type=int, default=300,
                        help='Seconds between scans (default: 300 = 5 min)')
    parser.add_argument('--max-iterations', type=int, default=None,
                        help='Maximum iterations (default: infinite)')
    parser.add_argument('--simulated-balance', type=float, default=None,
                        help='Simulated balance for dry-run testing')
    parser.add_argument('--min-edge', type=float, default=0.15,
                        help='Min edge to trade (default: 0.15 = 15%%)')
    parser.add_argument('--max-markets', type=int, default=10,
                        help='Max markets to evaluate per scan (default: 10)')

    args = parser.parse_args()

    risk_config = RiskConfig()
    directional_config = DirectionalConfig(
        min_edge=args.min_edge,
        max_markets_to_evaluate=args.max_markets
    )

    dry_run = not args.live

    trader = DirectionalTrader(
        risk_config=risk_config,
        directional_config=directional_config,
        dry_run=dry_run,
        simulated_balance=args.simulated_balance
    )
    trader.run(scan_interval=args.scan_interval, max_iterations=args.max_iterations)


if __name__ == "__main__":
    main()
