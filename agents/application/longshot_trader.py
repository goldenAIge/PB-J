"""
Longshot Seller Strategy for Polymarket

Strategy: Exploit the favorite-longshot bias by buying NO on overpriced low-probability events.
- Find markets where YES is priced $0.03-0.20 (longshots)
- Longshots are systematically overpriced due to behavioral bias
- Buy NO shares at $0.80-0.97, collecting $1.00 at resolution
- Diversify across uncorrelated events to smooth equity curve

Key features:
- Scans 500+ markets per cycle for longshot candidates
- Category-aware diversification (max exposure per category)
- Time-decay edge: events approaching resolution with no progress
- Crypto-specific heuristic: price distance from current as signal
- GTC limit orders = zero maker fees
- Resolution verification scoreboard
- Balance/risk checks via shared PortfolioRiskManager
- Market cooldowns after failures

The math:
- Buy NO at $0.90 → win $0.10, lose $0.90. Breakeven: 90%
- If true probability of YES is 5% (not 10% as market implies):
  → 95% win rate >> 90% breakeven → +EV
- Diversified across 10-20 uncorrelated events → smooth equity curve

Usage:
    python -m agents.application.longshot_trader [--dry-run] [--live] [--scan-interval 300]
"""

import os
import sys
import time
import json
import asyncio
import logging
import argparse
from datetime import datetime, timezone, timedelta
from typing import Optional
from dataclasses import dataclass, asdict

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from agents.polymarket.polymarket import Polymarket
from agents.polymarket.gamma import GammaMarketClient
from agents.application.risk_manager import RiskConfig, PortfolioRiskManager
from agents.connectors.telegram_alerts import TelegramAlerter

load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('longshot_trades.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────
# Category keywords for diversification tracking
# ────────────────────────────────────────────────────────────
# ────────────────────────────────────────────────────────────
# Crypto threshold market detection
# Markets like "ETH above $2,000" near current price are NOT longshots.
# They reflect genuine uncertainty around the current spot price.
# Only trade crypto thresholds when YES is very low (strike far from spot).
# ────────────────────────────────────────────────────────────
CRYPTO_ASSETS = ["bitcoin", "btc", "ethereum", "eth", "solana", "sol",
                 "xrp", "dogecoin", "doge", "cardano", "ada", "bnb"]

CRYPTO_THRESHOLD_PATTERNS = ["above $", "reach $", "dip to $", "below $",
                             "hit $", "drop to $", "fall to $", "rise to $",
                             "price of"]

# Max YES price for crypto threshold markets — anything above this
# means the strike is near the current spot price and NOT a longshot
CRYPTO_THRESHOLD_MAX_YES = 0.05


def is_crypto_threshold_market(question: str) -> bool:
    """Detect crypto price threshold markets (e.g., 'Will ETH be above $2,000?')"""
    q = question.lower()
    has_crypto = any(asset in q for asset in CRYPTO_ASSETS)
    has_threshold = any(pattern in q for pattern in CRYPTO_THRESHOLD_PATTERNS)
    return has_crypto and has_threshold


CATEGORY_KEYWORDS = {
    "crypto_btc": ["bitcoin", "btc"],
    "crypto_eth": ["ethereum", "eth"],
    "crypto_sol": ["solana", "sol"],
    "crypto_other": ["crypto", "token", "coin", "fdv", "market cap"],
    "politics_us": ["trump", "biden", "congress", "senate", "house", "republican", "democrat", "fed chair", "nomination"],
    "geopolitics": ["iran", "ukraine", "russia", "china", "war", "strike", "nato", "sanctions"],
    "sports": ["nba", "nfl", "mlb", "nhl", "ufc", "premier league", "champions league", "world cup", "fifa"],
    "tech": ["ai ", "openai", "google", "apple", "tesla", "spacex", "launch"],
    "elon": ["musk", "elon", "doge", "x.com", "tweet"],
}


def categorize_market(question: str) -> str:
    """Assign a category to a market based on question keywords."""
    q_lower = question.lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in q_lower for kw in keywords):
            return category
    return "other"


class LongshotConfig(BaseModel):
    """Configuration for longshot selling strategy"""
    # YES price range to target (these are the longshots we sell against)
    min_yes_price: float = Field(default=0.03, description="Min YES price to consider")
    max_yes_price: float = Field(default=0.20, description="Max YES price to consider")

    # This means NO price will be $0.80-0.97
    # Edge estimation
    longshot_bias_factor: float = Field(default=0.60, description="Assume true prob is this fraction of market price")
    min_edge: float = Field(default=0.03, description="Min estimated edge (EV per $1) to trade")

    # Diversification
    max_positions_per_category: int = Field(default=3, description="Max open positions per category")
    max_total_positions: int = Field(default=20, description="Max total open positions")
    min_categories: int = Field(default=2, description="Require positions in at least N categories before concentrating")

    # Position sizing
    trade_percent: float = Field(default=0.05, description="% of balance per trade (5%)")
    min_trade_size: float = Field(default=5.0, description="Min trade in USDC")
    max_trade_size: float = Field(default=25.0, description="Max trade in USDC")

    # Market quality filters
    min_volume_24h: float = Field(default=500, description="Min 24h volume in USD")
    min_liquidity: float = Field(default=1000, description="Min liquidity in USD")
    max_spread: float = Field(default=0.04, description="Max order book spread for NO side")

    # Time filters
    min_hours_to_resolution: float = Field(default=6, description="Don't buy if resolving in <6h (late entry risk)")
    max_days_to_resolution: float = Field(default=30, description="Don't buy if resolving >30 days out (capital lock)")
    prefer_short_resolution: bool = Field(default=True, description="Prioritize faster-resolving markets")

    # Execution
    scan_interval: float = Field(default=300, description="Seconds between scans (5 min default)")
    max_trades_per_cycle: int = Field(default=3, description="Max new positions per scan cycle")
    order_timeout: float = Field(default=30.0, description="Cancel unfilled limit orders after N seconds")

    # Market scan depth
    scan_limit: int = Field(default=500, description="Number of markets to scan per cycle")


@dataclass
class LongshotOpportunity:
    """A detected longshot selling opportunity"""
    market_id: str
    question: str
    yes_price: float          # The longshot YES price (what we're betting against)
    no_price: float           # What we'd pay for NO
    no_token_id: str          # Token ID for the NO side
    estimated_true_prob: float  # Our estimate of actual YES probability
    estimated_edge: float     # Expected value per $1 of NO
    category: str             # For diversification tracking
    volume_24h: float
    liquidity: float
    hours_to_resolution: Optional[float]
    end_date: str
    reason: str
    timestamp: str


@dataclass
class LongshotTrade:
    """Record of an executed longshot NO trade"""
    timestamp: str
    market_id: str
    question: str
    side: str                 # Always "NO"
    amount: float             # USDC spent
    entry_price: float        # NO price paid
    shares: float             # NO shares received
    expected_payout: float    # shares * $1.00
    expected_profit: float    # expected_payout - amount
    category: str
    estimated_edge: float
    status: str               # "PENDING", "FILLED", "DRY_RUN", "TIMEOUT_CANCELLED"
    # Resolution fields
    end_date: Optional[str] = None
    no_token_id: Optional[str] = None
    resolved_outcome: Optional[str] = None  # "YES" or "NO" after resolution
    actual_profit: Optional[float] = None
    resolution_status: str = "pending"       # "pending", "win", "loss"
    tx_hash: Optional[str] = None


class LongshotTrader:
    """
    Longshot Selling Bot for Polymarket

    Exploits the favorite-longshot bias:
    1. Scans all markets for low-probability events (YES at $0.03-0.20)
    2. Estimates true probability using longshot bias discount
    3. Buys NO shares when estimated edge exceeds threshold
    4. Diversifies across uncorrelated categories
    5. Collects $1.00 at resolution when the event (predictably) doesn't happen
    """

    def __init__(
        self,
        risk_config: Optional[RiskConfig] = None,
        longshot_config: Optional[LongshotConfig] = None,
        dry_run: bool = True,
        simulated_balance: Optional[float] = None
    ):
        self.polymarket = Polymarket()
        self.gamma = GammaMarketClient()
        self.config = longshot_config or LongshotConfig()
        self.dry_run = dry_run
        self.simulated_balance = simulated_balance

        # Shared risk manager
        risk_cfg = risk_config or RiskConfig(
            max_trade_percent=self.config.trade_percent,
            max_positions=self.config.max_total_positions,
            min_trade_size=self.config.min_trade_size,
            max_trade_size=self.config.max_trade_size,
            min_liquidity_usd=self.config.min_liquidity,
            min_volume_24h=self.config.min_volume_24h,
        )
        self.risk_manager = PortfolioRiskManager(self.polymarket, risk_cfg)

        # Telegram alerts
        self.telegram = TelegramAlerter()

        # Trading state
        self.initial_balance: Optional[float] = None
        self.current_balance: float = 0.0
        self.trade_history: list[LongshotTrade] = []
        self.pending_resolutions: list[LongshotTrade] = []

        # Resolution tracking
        self.verified_wins: int = 0
        self.verified_losses: int = 0
        self.verified_pnl: float = 0.0

        # Diversification tracking: category -> count of open positions
        self.category_exposure: dict[str, int] = {}

        # Already traded markets (avoid duplicates)
        self.seen_markets: set[str] = set()

        logger.info(f"LongshotTrader initialized (dry_run={dry_run})")
        if simulated_balance:
            logger.info(f"Using SIMULATED balance: ${simulated_balance:.2f}")
        logger.info(f"Config: YES range ${self.config.min_yes_price}-${self.config.max_yes_price} | "
                     f"Bias factor: {self.config.longshot_bias_factor} | "
                     f"Min edge: {self.config.min_edge}")

    # ────────────────────────────────────────────────────────
    # Balance & Portfolio
    # ────────────────────────────────────────────────────────

    def get_wallet_balance(self) -> float:
        """Get current USDC balance."""
        if self.simulated_balance is not None:
            return self.simulated_balance
        return self.risk_manager.get_balance()

    def initialize_balance(self) -> bool:
        """Initialize starting balance."""
        self.current_balance = self.get_wallet_balance()
        if self.initial_balance is None:
            self.initial_balance = self.current_balance

        logger.info(f"USDC balance: ${self.current_balance:.2f}")

        open_positions = sum(self.category_exposure.values())
        logger.info(f"Open positions: {open_positions}/{self.config.max_total_positions} "
                     f"across {len([c for c, n in self.category_exposure.items() if n > 0])} categories")

        if self.current_balance < self.config.min_trade_size:
            logger.info(f"Balance below min trade size (${self.config.min_trade_size}) — waiting")
            return False
        return True

    # ────────────────────────────────────────────────────────
    # Market Scanning
    # ────────────────────────────────────────────────────────

    def scan_for_opportunities(self) -> list[LongshotOpportunity]:
        """Scan all active markets for longshot selling opportunities."""
        opportunities = []

        try:
            logger.info(f"Scanning {self.config.scan_limit} markets for longshot opportunities...")
            markets = self.gamma.get_tradeable_markets(
                limit=self.config.scan_limit,
                order_by="volume24hr"
            )
            logger.info(f"Analyzing {len(markets)} markets...")

            for market in markets:
                opp = self.analyze_market(market)
                if opp and opp.market_id not in self.seen_markets:
                    # Check category diversification
                    current_in_category = self.category_exposure.get(opp.category, 0)
                    if current_in_category >= self.config.max_positions_per_category:
                        logger.debug(f"Skipping {opp.question[:40]}: category '{opp.category}' full ({current_in_category})")
                        continue
                    opportunities.append(opp)

            if opportunities:
                # Sort by edge (highest first), then prefer shorter resolution
                opportunities.sort(
                    key=lambda x: (
                        x.estimated_edge,
                        -1 * (x.hours_to_resolution or 9999),  # Shorter resolution = better
                    ),
                    reverse=True
                )

                logger.info(f"Found {len(opportunities)} longshot opportunities!")
                for opp in opportunities[:8]:
                    logger.info(f"  YES=${opp.yes_price:.2f} → NO=${opp.no_price:.2f} | "
                                f"Edge: {opp.estimated_edge:+.4f} | "
                                f"[{opp.category}] {opp.question[:50]}")
                    if opp.hours_to_resolution:
                        logger.info(f"    Resolves in {opp.hours_to_resolution:.0f}h | "
                                    f"Vol24h: ${opp.volume_24h:,.0f} | {opp.reason}")
            else:
                logger.info("No longshot opportunities found")

        except Exception as e:
            logger.error(f"Error scanning markets: {e}", exc_info=True)

        return opportunities

    def analyze_market(self, market: dict) -> Optional[LongshotOpportunity]:
        """Analyze a single market for longshot selling opportunity."""
        try:
            question = market.get('question', '')
            if not question:
                return None

            market_id = str(market.get('id', ''))

            # Check cooldown
            if self.risk_manager.is_market_on_cooldown(market_id):
                return None

            # Volume/liquidity filters
            volume_24h = float(market.get('volume24hr', 0) or 0)
            liquidity = float(market.get('liquidityClob', 0) or market.get('liquidity', 0) or 0)

            if volume_24h < self.config.min_volume_24h:
                return None
            if liquidity < self.config.min_liquidity:
                return None

            # Must have order book and be accepting orders
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

            # Parse outcomes (needed for non-YES/NO markets like sports)
            outcomes = market.get('outcomes', '[]')
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)

            # Get token IDs
            clob_token_ids = market.get('clobTokenIds', '[]')
            if isinstance(clob_token_ids, str):
                clob_token_ids = json.loads(clob_token_ids)
            if len(clob_token_ids) != 2:
                return None

            # We want markets where one side is a longshot ($0.03-0.20)
            # We'll buy the OTHER side (the favorite/NO)
            target_yes = None
            target_no = None
            no_token_id = None

            if self.config.min_yes_price <= yes_price <= self.config.max_yes_price:
                target_yes = yes_price
                target_no = no_price
                no_token_id = clob_token_ids[1]  # NO token
            elif self.config.min_yes_price <= no_price <= self.config.max_yes_price:
                # The "NO" side is the longshot — so we buy YES
                # Flip perspective: buying YES is buying the favorite
                target_yes = no_price
                target_no = yes_price
                no_token_id = clob_token_ids[0]  # YES token (which is the favorite here)

            if target_yes is None:
                return None

            # ── Crypto threshold filter ──
            # Markets like "ETH above $2,000" near current price are NOT longshots.
            # They reflect genuine uncertainty around the spot price.
            # Only trade crypto thresholds when YES <= 0.05 (strike far from spot).
            if is_crypto_threshold_market(question):
                if target_yes > CRYPTO_THRESHOLD_MAX_YES:
                    logger.debug(f"Rejecting crypto threshold near money: {question[:50]} YES=${target_yes:.2f}")
                    return None

            # ── Time filters ──
            hours_to_resolution = None
            end_date_str = market.get('endDate', '')
            if end_date_str:
                try:
                    end_date = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
                    now = datetime.now(timezone.utc)
                    hours_to_resolution = (end_date - now).total_seconds() / 3600

                    # Too close to resolution — risky entry
                    if 0 < hours_to_resolution < self.config.min_hours_to_resolution:
                        return None

                    # Too far out — capital locked too long
                    max_hours = self.config.max_days_to_resolution * 24
                    if hours_to_resolution > max_hours:
                        return None

                    # Already past end date — might be resolving, skip for safety
                    if hours_to_resolution < -48:
                        return None
                except Exception:
                    pass

            # ── Edge estimation ──
            # Core thesis: longshots are overpriced. True probability is ~60-70% of market price.
            estimated_true_prob = target_yes * self.config.longshot_bias_factor

            # Additional discount for extreme longshots (market dynamics favor overpricing more)
            if target_yes <= 0.05:
                estimated_true_prob *= 0.7  # Extra discount for very low-prob events
            elif target_yes <= 0.08:
                estimated_true_prob *= 0.85

            # Time decay bonus: events nearing resolution with low YES price are more likely NO
            if hours_to_resolution is not None and 0 < hours_to_resolution < 48:
                # Within 48h of resolution, events that haven't happened yet are less likely to
                time_decay_factor = max(0.5, hours_to_resolution / 48)
                estimated_true_prob *= time_decay_factor

            # EV calculation for buying NO at target_no price
            est_no_win_rate = 1.0 - estimated_true_prob
            ev_per_share = (est_no_win_rate * (1.0 - target_no)) - (estimated_true_prob * target_no)

            if ev_per_share < self.config.min_edge:
                return None

            # ── Category ──
            category = categorize_market(question)

            # ── Build reason string ──
            reasons = []
            reasons.append(f"YES@${target_yes:.2f} → est true prob {estimated_true_prob:.1%}")
            if hours_to_resolution and 0 < hours_to_resolution < 48:
                reasons.append(f"time decay ({hours_to_resolution:.0f}h left)")
            if target_yes <= 0.05:
                reasons.append("extreme longshot discount")

            # ── Order book pre-check ──
            try:
                ob_info = self.risk_manager.get_order_book_depth(no_token_id, "BUY", target_no)
                if ob_info is not None:
                    if ob_info.spread > self.config.max_spread:
                        return None
                    if ob_info.depth < 30:
                        return None
            except Exception:
                pass  # Let through to execution-time check

            return LongshotOpportunity(
                market_id=market_id,
                question=question,
                yes_price=target_yes,
                no_price=target_no,
                no_token_id=no_token_id,
                estimated_true_prob=estimated_true_prob,
                estimated_edge=ev_per_share,
                category=category,
                volume_24h=volume_24h,
                liquidity=liquidity,
                hours_to_resolution=hours_to_resolution,
                end_date=end_date_str,
                reason="; ".join(reasons),
                timestamp=datetime.now(timezone.utc).isoformat(),
            )

        except Exception as e:
            logger.debug(f"Error analyzing market: {e}")
            return None

    # ────────────────────────────────────────────────────────
    # Trade Execution
    # ────────────────────────────────────────────────────────

    def execute_trade(self, opp: LongshotOpportunity) -> Optional[LongshotTrade]:
        """Execute a longshot NO trade."""
        try:
            # Position sizing
            balance = self.get_wallet_balance()
            trade_size = min(
                balance * self.config.trade_percent,
                self.config.max_trade_size,
            )
            trade_size = max(trade_size, self.config.min_trade_size)

            if trade_size > balance * 0.30:
                logger.warning(f"Trade size ${trade_size:.2f} exceeds 30% of balance — reducing")
                trade_size = balance * 0.30

            if trade_size < self.config.min_trade_size:
                logger.info(f"Trade size ${trade_size:.2f} below minimum — skipping")
                return None

            # Re-verify order book price
            best_ask = self.polymarket.get_best_ask(opp.no_token_id)
            if best_ask is None:
                logger.warning(f"Cannot get NO best ask for {opp.question[:40]} — skipping")
                self.risk_manager.add_market_cooldown(opp.market_id)
                return None

            # Price drift check
            if abs(best_ask - opp.no_price) > 0.03:
                logger.warning(f"Price drifted: scan=${opp.no_price:.3f} → book=${best_ask:.3f} — skipping")
                return None

            # Don't buy NO above $0.97 (too little profit margin)
            if best_ask > 0.97:
                logger.info(f"NO price ${best_ask:.3f} too high — margin too thin")
                return None

            order_price = best_ask
            shares = trade_size / order_price

            trade = LongshotTrade(
                timestamp=datetime.now(timezone.utc).isoformat(),
                market_id=opp.market_id,
                question=opp.question,
                side="NO",
                amount=trade_size,
                entry_price=order_price,
                shares=shares,
                expected_payout=shares * 1.0,
                expected_profit=(shares * 1.0) - trade_size,
                category=opp.category,
                estimated_edge=opp.estimated_edge,
                status="DRY_RUN" if self.dry_run else "PENDING",
                end_date=opp.end_date,
                no_token_id=opp.no_token_id,
            )

            if self.dry_run:
                logger.info(f"[DRY RUN] Would buy NO @ ${order_price:.3f} | ${trade_size:.2f} → "
                            f"{shares:.1f} shares | Profit: ${trade.expected_profit:.2f} | "
                            f"[{opp.category}] {opp.question[:50]}")
                if self.simulated_balance is not None:
                    self.simulated_balance -= trade_size
                self._record_trade(trade, opp)
                return trade

            # Live execution — GTC limit buy (zero maker fees)
            logger.info(f"EXECUTING: Buy NO @ ${order_price:.3f} | ${trade_size:.2f} | {opp.question[:50]}")
            order_response = self.polymarket.execute_limit_buy(
                token_id=opp.no_token_id,
                price=order_price,
                size=shares,
            )

            if order_response and order_response.get('orderID'):
                order_id = order_response['orderID']
                trade.tx_hash = order_id
                trade.status = "FILLED"  # Optimistic — could add fill monitoring

                logger.info(f"ORDER PLACED: {order_id} | NO @ ${order_price:.3f} | "
                            f"${trade_size:.2f} → {shares:.1f} shares")

                # Wait briefly for fill
                time.sleep(2)

                self._record_trade(trade, opp)

                # Telegram alert
                self._send_alert(trade, opp)

                return trade
            else:
                logger.error(f"Order failed for {opp.question[:40]}")
                self.risk_manager.add_market_cooldown(opp.market_id)
                trade.status = "FAILED"
                return None

        except Exception as e:
            logger.error(f"Execution error: {e}", exc_info=True)
            self.risk_manager.add_market_cooldown(opp.market_id)
            return None

    def _record_trade(self, trade: LongshotTrade, opp: LongshotOpportunity):
        """Record trade and update tracking state."""
        self.trade_history.append(trade)
        self.pending_resolutions.append(trade)
        self.seen_markets.add(opp.market_id)

        # Update category exposure
        self.category_exposure[opp.category] = self.category_exposure.get(opp.category, 0) + 1

    def _send_alert(self, trade: LongshotTrade, opp: LongshotOpportunity):
        """Send Telegram alert for executed trade."""
        try:
            msg = (
                f"🎯 LONGSHOT NO TRADE\n"
                f"Market: {opp.question[:60]}\n"
                f"YES@${opp.yes_price:.2f} → Bought NO@${trade.entry_price:.3f}\n"
                f"Size: ${trade.amount:.2f} → {trade.shares:.1f} shares\n"
                f"Expected profit: ${trade.expected_profit:.2f}\n"
                f"Edge: {opp.estimated_edge:+.4f} | Cat: {opp.category}\n"
                f"Resolves: {opp.end_date[:10] if opp.end_date else '?'}"
            )
            self.telegram.send_message_sync(msg)
        except Exception:
            pass  # Don't let alert failures block trading

    # ────────────────────────────────────────────────────────
    # Resolution Verification
    # ────────────────────────────────────────────────────────

    def check_resolutions(self):
        """Check if any pending trades have resolved."""
        if not self.pending_resolutions:
            return

        still_pending = []
        for trade in self.pending_resolutions:
            try:
                # Re-fetch market from Gamma
                markets = self.gamma.get_markets(querystring_params={"id": trade.market_id})
                if not markets:
                    still_pending.append(trade)
                    continue

                market = markets[0] if isinstance(markets, list) else markets

                is_closed = market.get('closed', False)
                if not is_closed:
                    still_pending.append(trade)
                    continue

                # Market resolved — determine outcome
                outcome_prices = market.get('outcomePrices', '[]')
                if isinstance(outcome_prices, str):
                    outcome_prices = json.loads(outcome_prices)

                if len(outcome_prices) < 2:
                    still_pending.append(trade)
                    continue

                yes_final = float(outcome_prices[0])

                # If YES resolved to ~0 (event didn't happen) → our NO wins
                if yes_final < 0.01:
                    trade.resolution_status = "win"
                    trade.resolved_outcome = "NO"
                    trade.actual_profit = trade.expected_profit
                    self.verified_wins += 1
                    self.verified_pnl += trade.actual_profit
                    logger.info(f"✓ WIN: {trade.question[:50]} | +${trade.actual_profit:.2f}")

                    # Release category slot
                    if trade.category in self.category_exposure:
                        self.category_exposure[trade.category] = max(0, self.category_exposure[trade.category] - 1)

                    if self.simulated_balance is not None:
                        self.simulated_balance += trade.amount + trade.actual_profit

                elif yes_final > 0.99:
                    trade.resolution_status = "loss"
                    trade.resolved_outcome = "YES"
                    trade.actual_profit = -trade.amount
                    self.verified_losses += 1
                    self.verified_pnl += trade.actual_profit
                    logger.info(f"✗ LOSS: {trade.question[:50]} | -${trade.amount:.2f}")

                    if trade.category in self.category_exposure:
                        self.category_exposure[trade.category] = max(0, self.category_exposure[trade.category] - 1)
                else:
                    # Partial resolution or unclear — keep pending
                    still_pending.append(trade)
                    continue

            except Exception as e:
                logger.debug(f"Resolution check error: {e}")
                still_pending.append(trade)

        self.pending_resolutions = still_pending

        if self.verified_wins + self.verified_losses > 0:
            total = self.verified_wins + self.verified_losses
            wr = self.verified_wins / total * 100
            logger.info(f"SCOREBOARD: {self.verified_wins}W/{self.verified_losses}L ({wr:.1f}%) | "
                        f"PnL: ${self.verified_pnl:+.2f}")

    # ────────────────────────────────────────────────────────
    # Main Loop
    # ────────────────────────────────────────────────────────

    def print_status(self):
        """Print current trading status."""
        total_trades = len(self.trade_history)
        pending = len(self.pending_resolutions)
        resolved = self.verified_wins + self.verified_losses

        logger.info("=" * 60)
        logger.info(f"LONGSHOT TRADER STATUS")
        logger.info(f"  Balance: ${self.get_wallet_balance():.2f}")
        logger.info(f"  Trades: {total_trades} total | {pending} pending | {resolved} resolved")
        if resolved > 0:
            wr = self.verified_wins / resolved * 100
            logger.info(f"  Record: {self.verified_wins}W/{self.verified_losses}L ({wr:.1f}%)")
            logger.info(f"  Verified PnL: ${self.verified_pnl:+.2f}")
        logger.info(f"  Category exposure: {dict(self.category_exposure)}")
        logger.info("=" * 60)

    async def run(self, max_iterations: Optional[int] = None):
        """Main trading loop."""
        logger.info("Starting Longshot Trader...")
        iteration = 0

        while True:
            if max_iterations and iteration >= max_iterations:
                logger.info(f"Reached max iterations ({max_iterations})")
                break

            iteration += 1
            logger.info(f"\n--- Scan #{iteration} ---")

            # Initialize/update balance
            if not self.initialize_balance():
                logger.info(f"Waiting {self.config.scan_interval}s before next scan...")
                await asyncio.sleep(self.config.scan_interval)
                continue

            # Check pending resolutions
            self.check_resolutions()

            # Check if we have room for more positions
            total_open = sum(self.category_exposure.values())
            if total_open >= self.config.max_total_positions:
                logger.info(f"At max positions ({total_open}/{self.config.max_total_positions}) — waiting")
                self.print_status()
                await asyncio.sleep(self.config.scan_interval)
                continue

            # Scan for opportunities
            opportunities = self.scan_for_opportunities()

            # Execute top opportunities (up to max per cycle)
            trades_this_cycle = 0
            for opp in opportunities:
                if trades_this_cycle >= self.config.max_trades_per_cycle:
                    break

                # Re-check diversification
                if self.category_exposure.get(opp.category, 0) >= self.config.max_positions_per_category:
                    continue

                trade = self.execute_trade(opp)
                if trade and trade.status in ("FILLED", "DRY_RUN"):
                    trades_this_cycle += 1

            # Status update
            self.print_status()

            # Sleep until next scan
            logger.info(f"Next scan in {self.config.scan_interval}s...")
            await asyncio.sleep(self.config.scan_interval)


def main():
    parser = argparse.ArgumentParser(description="Longshot Seller — Polymarket NO bias trader")
    parser.add_argument("--dry-run", action="store_true", help="Simulate trades without executing")
    parser.add_argument("--live", action="store_true", help="Live trading mode")
    parser.add_argument("--scan-interval", type=float, default=300, help="Seconds between scans (default: 300)")
    parser.add_argument("--max-iterations", type=int, default=None, help="Stop after N scan cycles")
    parser.add_argument("--balance", type=float, default=None, help="Simulated starting balance")
    parser.add_argument("--max-yes-price", type=float, default=0.20, help="Max YES price to target (default: 0.20)")
    parser.add_argument("--min-edge", type=float, default=0.03, help="Min estimated edge to trade (default: 0.03)")

    args = parser.parse_args()

    if not args.live and not args.dry_run:
        args.dry_run = True
        logger.info("No mode specified — defaulting to --dry-run")

    config = LongshotConfig(
        scan_interval=args.scan_interval,
        max_yes_price=args.max_yes_price,
        min_edge=args.min_edge,
    )

    trader = LongshotTrader(
        longshot_config=config,
        dry_run=args.dry_run if args.dry_run else not args.live,
        simulated_balance=args.balance,
    )

    asyncio.run(trader.run(max_iterations=args.max_iterations))


if __name__ == "__main__":
    main()
