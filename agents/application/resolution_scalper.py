"""
Resolution Scalping Strategy for Polymarket

Strategy: Profit from price lag on markets with known outcomes
- Find markets where the outcome is already determined (or 99% certain)
- Price hasn't yet reached $1.00 (e.g., YES at $0.95)
- Buy the winning side with GTC limit order (zero maker fees)
- Collect $1.00 at resolution

Key features:
- Price-trust confidence model (price IS the confidence signal)
- GTC limit orders = zero maker fees (vs 2% taker fees with FOK)
- Resolution verification scoreboard for dry-run validation
- Uses get_tradeable_markets() with 500-market scan
- Volume/liquidity filters to skip illiquid markets
- Order book verification before every trade
- Balance/risk checks via shared PortfolioRiskManager
- Market cooldowns after failures

Usage:
    python -m agents.application.resolution_scalper [--dry-run] [--scan-interval 60]
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
        logging.FileHandler('resolution_scalp_trades.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


class ScalpConfig(BaseModel):
    """Configuration for resolution scalping"""
    min_price_threshold: float = Field(default=0.88, description="Min price to consider (e.g., 0.88 = 88%)")
    max_price_threshold: float = Field(default=0.98, description="Max price (zero fees make thin margins viable)")
    min_profit_percent: float = Field(default=0.015, description="Min profit percent (1.5% — viable with zero fees)")
    hours_to_resolution: int = Field(default=24, description="Only consider markets resolving within N hours")
    confidence_threshold: float = Field(default=0.60, description="Min confidence that outcome is known")
    max_spread: float = Field(default=0.03, description="Max order book spread to accept")
    max_price_drift: float = Field(default=0.02, description="Max price drift from scan to execution")
    max_trade_percent: float = Field(default=0.15, description="Max % of cash balance per scalp trade (15%)")
    min_trade_size: float = Field(default=1.0, description="Minimum trade size in USDC")
    max_trade_size: float = Field(default=50.0, description="Maximum trade size in USDC")
    order_timeout: float = Field(default=15.0, description="Cancel unfilled limit orders after N seconds")
    # 15-min BTC focus: only trade short-timeframe BTC resolution markets (for testing before all-in)
    btc_15min_only: bool = Field(default=False, description="Restrict to 15-min style BTC resolution markets")
    min_minutes_to_resolution: float = Field(default=2.0, description="Min minutes until resolution (avoid too late)")
    max_minutes_to_resolution: float = Field(default=20.0, description="Max minutes until resolution (short window)")
    btc_15min_scan_limit: int = Field(default=500, description="When btc_15min_only: scan this many markets (15-min BTC often not in top 100)")


@dataclass
class ScalpOpportunity:
    """Represents a resolution scalping opportunity"""
    market_id: str
    question: str
    recommended_side: str  # "YES" or "NO"
    current_price: float
    potential_profit_percent: float
    resolution_date: Optional[str]
    confidence_score: float
    reason: str
    token_id: str
    timestamp: str
    volume_24h: float = 0.0
    liquidity: float = 0.0


@dataclass
class ScalpTrade:
    """Record of an executed scalp trade"""
    timestamp: str
    market_id: str
    question: str
    side: str
    amount: float
    entry_price: float
    expected_payout: float
    expected_profit: float
    confidence: float
    reason: str
    status: str
    tx_hash: Optional[str] = None
    # Resolution verification fields
    end_date: Optional[str] = None
    resolved_side: Optional[str] = None  # Which side won after resolution
    actual_profit: Optional[float] = None  # Real P&L after resolution
    resolution_status: str = "pending"  # "pending", "win", "loss", "unknown"
    token_id: Optional[str] = None  # For re-querying


class ResolutionScalper:
    """
    Resolution Scalping Bot for Polymarket

    Finds markets where:
    1. Outcome is already known or highly certain
    2. Price hasn't reached $1.00 yet
    3. Profit margin exists after fees
    4. Sufficient liquidity and order book depth
    """

    def __init__(
        self,
        risk_config: Optional[RiskConfig] = None,
        scalp_config: Optional[ScalpConfig] = None,
        dry_run: bool = True,
        simulated_balance: Optional[float] = None
    ):
        self.polymarket = Polymarket()
        self.gamma = GammaMarketClient()
        self.risk_config = risk_config or RiskConfig()
        self.scalp_config = scalp_config or ScalpConfig()
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
        self.failed_trades: int = 0
        self.trade_history: list[ScalpTrade] = []

        # Resolution verification
        self.pending_resolutions: list[ScalpTrade] = []
        self.verified_wins: int = 0
        self.verified_losses: int = 0
        self.verified_pnl: float = 0.0

        # Tracking already-seen opportunities to avoid duplicates
        self.seen_markets: set[str] = set()

        logger.info(f"ResolutionScalper initialized (dry_run={dry_run})")
        if simulated_balance:
            logger.info(f"Using SIMULATED balance: ${simulated_balance:.2f}")
        if self.scalp_config.btc_15min_only:
            logger.info(f"15-min BTC only: resolution window {self.scalp_config.min_minutes_to_resolution}-{self.scalp_config.max_minutes_to_resolution} min")
        logger.info(f"Scalp config: {self.scalp_config.model_dump()}")

    def get_wallet_balance(self) -> float:
        """Get current USDC balance"""
        if self.simulated_balance is not None:
            return self.simulated_balance
        return self.risk_manager.get_balance()

    def initialize_balance(self) -> bool:
        """Initialize starting balance and log portfolio status."""
        self.current_balance = self.get_wallet_balance()
        if self.initial_balance is None:
            self.initial_balance = self.current_balance

        logger.info(f"USDC cash: ${self.current_balance:.2f}")

        # Log portfolio overview (positions + cash)
        self._log_portfolio_status()

        if self.current_balance < self.risk_config.min_trade_size:
            logger.info(f"Cash below min trade size (${self.risk_config.min_trade_size}) — waiting for positions to resolve")
            return False
        return True

    def _log_portfolio_status(self):
        """Log current positions and total portfolio value."""
        try:
            import httpx
            wallet = self.polymarket.get_address_for_private_key()
            resp = httpx.get(
                f"https://data-api.polymarket.com/positions?user={wallet}",
                timeout=15
            )
            if resp.status_code != 200:
                return

            positions = resp.json()
            active = []
            total_value = 0.0
            total_cost = 0.0
            resolved_value = 0.0

            for pos in positions:
                size = float(pos.get('size', 0))
                if size <= 0:
                    continue
                avg_price = float(pos.get('avgPrice', 0))
                cur_price = float(pos.get('curPrice', 0) or 0)
                value = size * cur_price
                cost = size * avg_price
                total_value += value
                total_cost += cost

                # Positions at $1.00 are resolved/settling
                if cur_price >= 0.999:
                    resolved_value += value

                active.append({
                    'title': (pos.get('title', '') or pos.get('market', ''))[:45],
                    'side': pos.get('outcome', '?'),
                    'size': size,
                    'cost': cost,
                    'value': value,
                    'pnl': value - cost,
                    'cur_price': cur_price,
                })

            if active:
                total_pnl = total_value - total_cost
                portfolio = total_value + self.current_balance
                logger.info(f"Positions: {len(active)} open | Value: ${total_value:.2f} | PnL: ${total_pnl:+.2f}")
                logger.info(f"Portfolio total: ${portfolio:.2f} (cash ${self.current_balance:.2f} + positions ${total_value:.2f})")
                if resolved_value > 0:
                    logger.info(f"Settling soon: ~${resolved_value:.2f} from resolved positions")

        except Exception as e:
            logger.debug(f"Could not fetch portfolio status: {e}")

    def scan_for_opportunities(self) -> list[ScalpOpportunity]:
        """Scan for resolution scalping opportunities using filtered market query."""
        opportunities = []

        try:
            logger.info("Scanning for resolution scalping opportunities...")

            # Scan 500 markets for wide opportunity set (was 100)
            # When 15-min BTC only, use dedicated scan limit
            limit = self.scalp_config.btc_15min_scan_limit if self.scalp_config.btc_15min_only else 500
            markets = self.gamma.get_tradeable_markets(limit=limit, order_by="volume24hr")
            logger.info(f"Analyzing {len(markets)} tradeable markets (sorted by volume)...")

            for market in markets:
                opp = self.analyze_market(market)
                if opp and opp.market_id not in self.seen_markets:
                    opportunities.append(opp)

            if opportunities:
                # Sort by profit * confidence
                opportunities.sort(
                    key=lambda x: x.potential_profit_percent * x.confidence_score,
                    reverse=True
                )

                logger.info(f"Found {len(opportunities)} scalping opportunities!")
                for opp in opportunities[:5]:
                    logger.info(f"  - {opp.question[:50]}...")
                    logger.info(f"    Side: {opp.recommended_side} @ ${opp.current_price:.2f} | Profit: {opp.potential_profit_percent:.1%}")
                    logger.info(f"    Reason: {opp.reason}")
            else:
                logger.info("No scalping opportunities found")

        except Exception as e:
            logger.error(f"Error scanning markets: {e}")

        return opportunities

    def analyze_market(self, market: dict) -> Optional[ScalpOpportunity]:
        """Analyze a market for scalping opportunity with volume/liquidity filters."""
        try:
            question = market.get('question', '')
            if not question:
                return None

            market_id = str(market.get('id', ''))

            # Fix 5: Check cooldown
            if self.risk_manager.is_market_on_cooldown(market_id):
                return None

            # Fix 2: Volume/liquidity filters
            volume_24h = float(market.get('volume24hr', 0) or 0)
            liquidity = float(market.get('liquidityClob', 0) or market.get('liquidity', 0) or 0)

            if volume_24h < self.risk_config.min_volume_24h:
                return None
            if liquidity < self.risk_config.min_liquidity_usd:
                return None

            # Check order book is enabled and accepting orders
            if not market.get('enableOrderBook', False):
                return None
            if not market.get('acceptingOrders', True):
                return None

            # Optional: restrict to 15-min BTC resolution markets (for strategy testing)
            if self.scalp_config.btc_15min_only:
                q_lower = question.lower()
                if 'btc' not in q_lower and 'bitcoin' not in q_lower:
                    return None
                end_date_str = market.get('endDate', '')
                if not end_date_str:
                    return None
                try:
                    end_date_str_clean = end_date_str.replace('Z', '+00:00')
                    end_date = datetime.fromisoformat(end_date_str_clean)
                    now = datetime.now(end_date.tzinfo) if end_date.tzinfo else datetime.now()
                    minutes_to_resolution = (end_date - now).total_seconds() / 60
                    if minutes_to_resolution < self.scalp_config.min_minutes_to_resolution:
                        return None
                    if minutes_to_resolution > self.scalp_config.max_minutes_to_resolution:
                        return None
                except Exception:
                    return None

            # Parse prices
            outcome_prices = market.get('outcomePrices', '[]')
            if isinstance(outcome_prices, str):
                outcome_prices = json.loads(outcome_prices)

            if len(outcome_prices) != 2:
                return None

            yes_price = float(outcome_prices[0])
            no_price = float(outcome_prices[1])

            # Get token IDs
            clob_token_ids = market.get('clobTokenIds', '[]')
            if isinstance(clob_token_ids, str):
                clob_token_ids = json.loads(clob_token_ids)

            if len(clob_token_ids) != 2:
                return None

            # Check if either side is in our target range (0.90-0.99)
            yes_in_range = self.scalp_config.min_price_threshold <= yes_price <= self.scalp_config.max_price_threshold
            no_in_range = self.scalp_config.min_price_threshold <= no_price <= self.scalp_config.max_price_threshold

            if not yes_in_range and not no_in_range:
                return None

            # Determine which side to bet on
            if yes_in_range and (not no_in_range or yes_price > no_price):
                side = "YES"
                price = yes_price
                token_id = clob_token_ids[0]
            else:
                side = "NO"
                price = no_price
                token_id = clob_token_ids[1]

            # Calculate profit — GTC limit orders = zero maker fees
            gross_profit = 1.0 - price
            fees = 0.0  # Maker orders have zero fees on Polymarket
            net_profit = gross_profit - fees
            profit_percent = net_profit / price if price > 0 else 0

            if profit_percent < self.scalp_config.min_profit_percent:
                return None

            # Fix 3: Simplified confidence assessment (no circular price-as-confidence)
            confidence, reason = self.assess_outcome_confidence(market, side, price)

            if confidence < self.scalp_config.confidence_threshold:
                return None

            # Fix 4 (pre-scan): Order book check during scan phase
            # Reject markets with poor liquidity before they become opportunities
            try:
                ob_info = self.risk_manager.get_order_book_depth(token_id, "BUY", price)
                if ob_info is None:
                    return None
                if ob_info.spread > self.scalp_config.max_spread:
                    logger.debug(f"Skipping {question[:40]}: spread ${ob_info.spread:.4f} > max ${self.scalp_config.max_spread}")
                    return None
                if ob_info.depth < 50:
                    logger.debug(f"Skipping {question[:40]}: depth {ob_info.depth:.0f} shares too thin")
                    return None
            except Exception:
                pass  # If order book check fails, let it through to execution-time check

            return ScalpOpportunity(
                market_id=market_id,
                question=question,
                recommended_side=side,
                current_price=price,
                potential_profit_percent=profit_percent,
                resolution_date=market.get('endDate'),
                confidence_score=confidence,
                reason=reason,
                token_id=token_id,
                timestamp=datetime.now().isoformat(),
                volume_24h=volume_24h,
                liquidity=liquidity
            )

        except Exception as e:
            logger.debug(f"Error analyzing market: {e}")
            return None

    def assess_outcome_confidence(self, market: dict, side: str, price: float) -> tuple[float, str]:
        """
        Price-trust confidence model.

        Key insight: the market price IS the confidence signal. A market at $0.95
        reflects thousands of traders' consensus that the outcome has a 95% chance.
        We trust the crowd's assessment and layer on resolution timing + event type.

        Scoring:
        - Price-as-confidence (primary):  0.88→0.30, 0.90→0.40, 0.93→0.55, 0.95→0.65, 0.97→0.75
        - Resolution proximity (secondary): past→+0.20, <30min→+0.15, <2h→+0.10, <6h→+0.05, <24h→+0.02
        - Event type (bonus):              sports/election→+0.10, financial→+0.08
        - Past-tense keywords (minor):     strong→+0.08, weak→+0.03
        """
        question = market.get('question', '').lower()
        description = market.get('description', '').lower()
        end_date_str = market.get('endDate', '')

        confidence = 0.0
        reasons = []

        # ── Factor 1 (PRIMARY): Price-as-confidence ──
        # The market price reflects collective wisdom of all participants.
        # Higher price = more certain outcome.
        if price >= 0.97:
            confidence += 0.75
            reasons.append(f"Price ${price:.2f} (very high conviction)")
        elif price >= 0.95:
            confidence += 0.65
            reasons.append(f"Price ${price:.2f} (high conviction)")
        elif price >= 0.93:
            confidence += 0.55
            reasons.append(f"Price ${price:.2f} (moderate-high conviction)")
        elif price >= 0.90:
            confidence += 0.40
            reasons.append(f"Price ${price:.2f} (moderate conviction)")
        elif price >= 0.88:
            confidence += 0.30
            reasons.append(f"Price ${price:.2f} (lower conviction)")
        else:
            confidence += 0.20
            reasons.append(f"Price ${price:.2f} (low conviction)")

        # ── Factor 2: Resolution proximity ──
        hours_to_resolution = None
        if end_date_str:
            try:
                end_date_str_clean = end_date_str.replace('Z', '+00:00')
                end_date = datetime.fromisoformat(end_date_str_clean)
                now = datetime.now(end_date.tzinfo) if end_date.tzinfo else datetime.now()

                time_to_resolution = end_date - now
                hours_to_resolution = time_to_resolution.total_seconds() / 3600

                if hours_to_resolution < 0:
                    confidence += 0.20
                    reasons.append("Past end date — awaiting resolution")
                elif hours_to_resolution <= 0.5:
                    confidence += 0.15
                    reasons.append("Resolves within 30 minutes")
                elif hours_to_resolution <= 2:
                    confidence += 0.10
                    reasons.append("Resolves within 2 hours")
                elif hours_to_resolution <= 6:
                    confidence += 0.05
                    reasons.append("Resolves within 6 hours")
                elif hours_to_resolution <= 24:
                    confidence += 0.02
                    reasons.append("Resolves within 24 hours")
                else:
                    return 0.0, f"Resolves in {hours_to_resolution:.0f}h — too far out"
            except Exception:
                return 0.0, "Cannot parse resolution date"

        # ── Factor 3: Event type ──
        sports_keywords = [
            'game', 'match', 'championship', 'super bowl', 'world series',
            'nba', 'nfl', 'mlb', 'nhl', 'ufc', 'boxing', 'premier league',
            'score', 'goals', 'points', 'touchdown', 'first blood',
            'counter-strike', 'esports', 'map winner',
        ]
        election_keywords = [
            'election', 'vote', 'ballot', 'winner', 'elected',
            'seats', 'party', 'governor', 'mayor', 'senator',
        ]
        financial_keywords = [
            'close at', 'close above', 'close below', 'close between',
            'trading day', 'stock price', 'nflx', 'aapl', 'tsla',
            'spy', 'btc', 'eth', 'bitcoin', 'ethereum',
        ]

        is_sports = any(kw in question for kw in sports_keywords)
        is_election = any(kw in question for kw in election_keywords)
        is_financial = any(kw in question for kw in financial_keywords)

        if is_sports or is_election:
            confidence += 0.10
            reasons.append("Sports/election (verifiable outcome)")
        elif is_financial:
            confidence += 0.08
            reasons.append("Financial (verifiable at close)")

        # ── Factor 4: Past-tense keywords (minor bonus) ──
        strong_past_indicators = [
            'did ', 'won ', 'lost ', 'defeated ', 'beat ',
            'signed ', 'passed ', 'announced ', 'fired ',
            'ended', 'finished', 'concluded', 'elected',
        ]
        weak_past_indicators = [
            'was ', 'were ', 'has ', 'have ', 'happened', 'occurred',
        ]

        has_strong_past = any(kw in question for kw in strong_past_indicators)
        has_weak_past = any(kw in question or kw in description for kw in weak_past_indicators)

        if has_strong_past:
            confidence += 0.08
            reasons.append("Past-tense indicator (strong)")
        elif has_weak_past:
            confidence += 0.03
            reasons.append("Past-tense indicator (weak)")

        # Cap confidence at 1.0
        confidence = min(confidence, 1.0)

        reason = "; ".join(reasons) if reasons else "Price signal only"

        return confidence, reason

    def execute_scalp(self, opportunity: ScalpOpportunity) -> Optional[ScalpTrade]:
        """Execute a scalp trade with GTC limit order (zero fees), order book verification, and risk checks."""
        market_id = opportunity.market_id

        # Risk manager checks
        if self.risk_manager.is_market_on_cooldown(market_id):
            logger.info(f"Market {market_id} on cooldown, skipping")
            return None

        # Position sizing: fixed fraction of cash balance
        balance = self.risk_manager.get_balance()
        trade_size = balance * self.scalp_config.max_trade_percent
        trade_size = min(trade_size, self.scalp_config.max_trade_size)
        trade_size = max(trade_size, self.scalp_config.min_trade_size)

        if trade_size > balance:
            logger.warning(f"Insufficient balance: ${balance:.2f} < min trade ${self.scalp_config.min_trade_size}")
            return None

        logger.info(f"Trade size: ${trade_size:.2f} ({self.scalp_config.max_trade_percent:.0%} of ${balance:.2f})")

        # Check position count limit
        can_trade, reason = self.risk_manager.can_open_position(trade_size)
        if not can_trade:
            logger.warning(f"Risk check failed: {reason}")
            return None

        # Order book verification before execution
        ob_info = self.risk_manager.get_order_book_depth(
            opportunity.token_id, "BUY", opportunity.current_price
        )

        if ob_info is None:
            logger.warning("Cannot read order book, skipping trade")
            self.risk_manager.record_failed_market(market_id)
            return None

        if ob_info.spread > self.scalp_config.max_spread:
            logger.warning(f"Spread too wide: ${ob_info.spread:.4f} > ${self.scalp_config.max_spread}")
            return None

        # Check price hasn't drifted
        price_drift = abs(ob_info.best_price - opportunity.current_price)
        if price_drift > self.scalp_config.max_price_drift:
            logger.warning(f"Price drifted: scan=${opportunity.current_price:.4f} vs book=${ob_info.best_price:.4f}")
            return None

        # Check sufficient liquidity for our trade size
        shares_needed = trade_size / opportunity.current_price
        if ob_info.can_fill_amount < shares_needed * 0.8:
            logger.warning(f"Insufficient depth: {ob_info.can_fill_amount:.0f} shares vs {shares_needed:.0f} needed")
            return None

        # Calculate shares and expected payout — zero fees for GTC limit orders
        shares = trade_size / opportunity.current_price
        expected_payout = shares * 1.0
        expected_profit = expected_payout - trade_size  # No fees!

        trade = ScalpTrade(
            timestamp=datetime.now(timezone.utc).isoformat(),
            market_id=market_id,
            question=opportunity.question,
            side=opportunity.recommended_side,
            amount=trade_size,
            entry_price=opportunity.current_price,
            expected_payout=expected_payout,
            expected_profit=expected_profit,
            confidence=opportunity.confidence_score,
            reason=opportunity.reason,
            status="PENDING",
            end_date=opportunity.resolution_date,
            token_id=opportunity.token_id,
        )

        logger.info(f"\n{'='*60}")
        logger.info("EXECUTING RESOLUTION SCALP (GTC LIMIT ORDER)")
        logger.info(f"Market: {opportunity.question[:60]}...")
        logger.info(f"Side: {opportunity.recommended_side} @ ${opportunity.current_price:.4f}")
        logger.info(f"Order book: spread=${ob_info.spread:.4f}, depth={ob_info.depth:.0f}")
        logger.info(f"Confidence: {opportunity.confidence_score:.0%} | Reason: {opportunity.reason}")
        logger.info(f"Trade size: ${trade_size:.2f} | Expected profit: ${expected_profit:.2f} (zero fees!)")
        logger.info(f"{'='*60}\n")

        if self.dry_run:
            logger.info("[DRY RUN] GTC limit order simulated (not placed on chain)")
            trade.status = "DRY_RUN"
            self.successful_trades += 1
            self.seen_markets.add(market_id)
            # Queue for resolution verification instead of instant profit
            self.pending_resolutions.append(trade)
            logger.info(f"[DRY RUN] Trade queued for resolution verification (resolves at {opportunity.resolution_date})")
        else:
            try:
                logger.info(f"Placing GTC limit order: {shares:.2f} shares of {opportunity.recommended_side} @ ${opportunity.current_price:.4f}")
                logger.warning(">>> EXECUTING REAL TRADE - FUNDS WILL BE USED <<<")

                # Place GTC limit order (maker = zero fees)
                order_response = self.polymarket.execute_limit_buy(
                    token_id=opportunity.token_id,
                    price=opportunity.current_price,
                    size=round(shares, 2),
                )

                order_id = None
                if isinstance(order_response, dict):
                    order_id = order_response.get("orderID") or order_response.get("id")
                elif isinstance(order_response, str):
                    order_id = order_response

                trade.tx_hash = str(order_id) if order_id else None
                logger.info(f"Order placed: {order_id}")

                # Wait for fill or timeout
                filled = self._wait_for_fill_sync(order_id, opportunity.token_id)

                if filled:
                    trade.status = "FILLED"
                    self.successful_trades += 1
                    self.seen_markets.add(market_id)
                    logger.info("Order FILLED! Holding to resolution.")
                else:
                    # Cancel unfilled order
                    if order_id:
                        try:
                            self.polymarket.cancel_order(str(order_id))
                            logger.info(f"Order cancelled after {self.scalp_config.order_timeout}s timeout")
                        except Exception as e:
                            logger.warning(f"Failed to cancel order: {e}")
                    trade.status = "TIMEOUT_CANCELLED"
                    self.failed_trades += 1

            except Exception as e:
                error_msg = str(e).lower()

                if "balance" in error_msg or "allowance" in error_msg or "insufficient" in error_msg:
                    failure_reason = "Insufficient balance or allowance"
                    self.risk_manager.get_balance(force_refresh=True)
                    self.risk_manager.record_failed_market(market_id)
                elif "no match" in error_msg:
                    failure_reason = "No liquidity - price moved or taken"
                elif "size" in error_msg or "minimum" in error_msg:
                    failure_reason = "Order size below minimum"
                elif "timeout" in error_msg or "timed out" in error_msg:
                    failure_reason = "Network timeout"
                else:
                    failure_reason = str(e)

                logger.error(f"Trade execution failed: {failure_reason}")
                trade.status = "FAILED"
                trade.reason = failure_reason
                self.failed_trades += 1
                self.risk_manager.record_failed_market(market_id)

        self.total_trades += 1
        self.trade_history.append(trade)
        self.log_trade(trade)

        # Send Telegram alert only for successful fills (not routine failures)
        if trade.status in ("FILLED", "DRY_RUN"):
            try:
                self.telegram.send_message_sync(
                    f"{'[DRY] ' if self.dry_run else ''}<b>SCALP {trade.status}</b>\n\n"
                    f"{opportunity.question[:50]}...\n"
                    f"BUY {opportunity.recommended_side} @ ${opportunity.current_price:.4f}\n"
                    f"Amount: ${trade_size:.2f}\n"
                    f"Expected Profit: ${expected_profit:.2f} (zero fees!)\n"
                    f"Confidence: {opportunity.confidence_score:.0%}\n"
                    f"Spread: ${ob_info.spread:.4f}\n"
                )
            except Exception as e:
                logger.warning(f"Failed to send Telegram alert: {e}")

        return trade

    def _wait_for_fill_sync(self, order_id: Optional[str], token_id: str) -> bool:
        """Wait for a limit order to fill, up to order_timeout seconds (sync version)."""
        if not order_id:
            return False

        start = time.time()
        while (time.time() - start) < self.scalp_config.order_timeout:
            try:
                order = self.polymarket.get_order(str(order_id))
                status = ""
                if isinstance(order, dict):
                    status = order.get("status", "").lower()
                elif isinstance(order, str):
                    status = order.lower()

                if "matched" in status or "filled" in status:
                    return True
                if "cancelled" in status or "canceled" in status:
                    return False
            except Exception:
                pass
            time.sleep(1.0)

        return False

    def log_trade(self, trade: ScalpTrade):
        """Log trade to file"""
        try:
            with open('resolution_scalp_trades.log', 'a') as f:
                f.write(json.dumps(asdict(trade)) + '\n')
        except Exception as e:
            logger.error(f"Failed to log trade: {e}")

    def check_resolutions(self):
        """
        Check if any pending dry-run trades have resolved.

        Queries the Gamma API for each pending trade's market to see
        which side won, then scores our prediction.
        """
        if not self.pending_resolutions:
            return

        now = datetime.now(timezone.utc)
        still_pending = []

        for trade in self.pending_resolutions:
            if not trade.end_date:
                still_pending.append(trade)
                continue

            try:
                end_dt = datetime.fromisoformat(trade.end_date.replace("Z", "+00:00"))
            except Exception:
                still_pending.append(trade)
                continue

            # Wait 60s past endDate for resolution to propagate
            if now < end_dt + timedelta(seconds=60):
                still_pending.append(trade)
                continue

            # Market should have resolved — check outcome
            resolved_side = self._get_resolved_side(trade)

            if resolved_side is None:
                # Keep checking for up to 60 minutes past endDate OR trade placement
                # (for markets whose endDate already passed when we traded)
                try:
                    trade_dt = datetime.fromisoformat(trade.timestamp.replace("Z", "+00:00"))
                except Exception:
                    trade_dt = end_dt
                check_deadline = max(end_dt, trade_dt) + timedelta(minutes=60)
                if now < check_deadline:
                    still_pending.append(trade)
                else:
                    logger.warning(f"Could not verify resolution after 60 min for {trade.question[:50]}")
                    trade.resolution_status = "unknown"
                continue

            trade.resolved_side = resolved_side
            # For YES/NO markets: our side matches if it resolved to the same outcome
            won = (trade.side.lower() == resolved_side.lower())

            if won:
                trade.resolution_status = "win"
                trade.actual_profit = trade.expected_profit
                self.verified_wins += 1
                self.verified_pnl += trade.actual_profit
                if self.simulated_balance is not None:
                    self.simulated_balance += trade.actual_profit
                logger.info(
                    f"VERIFIED WIN: {trade.question[:50]} | "
                    f"Bet {trade.side}, resolved {resolved_side} | "
                    f"Profit: +${trade.actual_profit:.2f}"
                )
            else:
                trade.resolution_status = "loss"
                trade.actual_profit = -trade.amount
                self.verified_losses += 1
                self.verified_pnl += trade.actual_profit
                if self.simulated_balance is not None:
                    self.simulated_balance += trade.actual_profit
                logger.info(
                    f"VERIFIED LOSS: {trade.question[:50]} | "
                    f"Bet {trade.side}, resolved {resolved_side} | "
                    f"Loss: -${trade.amount:.2f}"
                )

            # Update trade log
            self.log_trade(trade)

            # Telegram notification for resolution
            total_resolved = self.verified_wins + self.verified_losses
            win_rate = self.verified_wins / total_resolved * 100 if total_resolved > 0 else 0
            try:
                self.telegram.send_message_sync(
                    f"{'[DRY] ' if self.dry_run else ''}<b>RESOLUTION {'WIN' if won else 'LOSS'}</b>\n\n"
                    f"{trade.question[:50]}\n"
                    f"Bet: {trade.side} | Result: {resolved_side}\n"
                    f"P&L: ${trade.actual_profit:+.2f}\n\n"
                    f"Score: {self.verified_wins}W/{self.verified_losses}L ({win_rate:.0f}%)\n"
                    f"Verified P&L: ${self.verified_pnl:+.2f}\n"
                )
            except Exception:
                pass

        self.pending_resolutions = still_pending

    def _get_resolved_side(self, trade: ScalpTrade) -> Optional[str]:
        """Query Gamma API to determine which side won for a market."""
        import httpx as _httpx

        try:
            # Query by market ID
            response = _httpx.get(
                f"https://gamma-api.polymarket.com/markets/{trade.market_id}",
                timeout=10,
            )
            if response.status_code != 200:
                return None

            market = response.json()

            outcome_prices = market.get("outcomePrices", "[]")
            if isinstance(outcome_prices, str):
                outcome_prices = json.loads(outcome_prices)

            outcomes = market.get("outcomes", "[]")
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)

            if len(outcome_prices) >= 2 and len(outcomes) >= 2:
                prices = [float(p) for p in outcome_prices]
                # Winner has price >= 0.95, loser <= 0.05
                if max(prices) >= 0.95 and min(prices) <= 0.05:
                    winner_idx = prices.index(max(prices))
                    return outcomes[winner_idx]

            return None

        except Exception as e:
            logger.debug(f"Error checking resolution for market {trade.market_id}: {e}")
            return None

    def print_status(self):
        """Print current status with resolution scoreboard"""
        logger.info(f"\n{'='*50}")
        logger.info("RESOLUTION SCALPER STATUS")
        logger.info(f"{'='*50}")
        logger.info(f"Balance: ${self.current_balance:.2f}")
        if self.initial_balance:
            profit = self.current_balance - self.initial_balance
            logger.info(f"Initial: ${self.initial_balance:.2f}")
            logger.info(f"P&L: ${profit:+.2f} ({profit/self.initial_balance:+.1%})")
        logger.info(f"Total trades: {self.total_trades}")
        logger.info(f"Successful: {self.successful_trades}")
        logger.info(f"Failed: {self.failed_trades}")

        # Resolution verification scoreboard
        total_resolved = self.verified_wins + self.verified_losses
        pending = len(self.pending_resolutions)
        if total_resolved > 0 or pending > 0:
            win_rate = self.verified_wins / total_resolved * 100 if total_resolved > 0 else 0
            logger.info(f"--- Resolution Scoreboard ---")
            logger.info(f"Verified: {self.verified_wins}W / {self.verified_losses}L ({win_rate:.0f}% win rate)")
            logger.info(f"Verified P&L: ${self.verified_pnl:+.2f}")
            logger.info(f"Awaiting resolution: {pending}")

        logger.info(f"Mode: {'DRY RUN' if self.dry_run else 'LIVE'}")
        logger.info(f"{'='*50}\n")

    def run(self, scan_interval: int = 120, max_iterations: Optional[int] = None):
        """Main trading loop"""
        logger.info(f"\n{'='*60}")
        logger.info("STARTING RESOLUTION SCALPER")
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

                has_capital = self.initialize_balance()

                if not has_capital:
                    # Try to redeem resolved positions to free up capital
                    try:
                        redeemable = self.polymarket.get_redeemable_positions()
                        if redeemable:
                            logger.info(f"Found {len(redeemable)} redeemable positions — redeeming...")
                            results = self.polymarket.redeem_positions()
                            redeemed = sum(1 for r in results if r["success"])
                            if redeemed > 0:
                                logger.info(f"Redeemed {redeemed} positions! Rechecking balance...")
                                has_capital = self.initialize_balance()
                    except Exception as e:
                        logger.warning(f"Auto-redeem failed: {e}")

                if has_capital:
                    # Scan for opportunities
                    opportunities = self.scan_for_opportunities()

                    # Execute best opportunity
                    if opportunities:
                        best = opportunities[0]  # Already sorted by profit * confidence
                        self.execute_scalp(best)
                else:
                    logger.info("Waiting for positions to resolve and free up capital...")

                # Check if any previous trades have resolved
                self.check_resolutions()

                self.print_status()

                # Wait longer when no capital (no point scanning aggressively)
                wait_time = scan_interval * 3 if not has_capital else scan_interval
                logger.info(f"Waiting {wait_time}s until next scan...")
                time.sleep(wait_time)

        except KeyboardInterrupt:
            logger.info("\nScalper stopped by user")
        except Exception as e:
            logger.error(f"Error in main loop: {e}")
        finally:
            self.print_status()
            logger.info("Resolution scalper stopped")


def main():
    parser = argparse.ArgumentParser(description='Polymarket Resolution Scalper')
    parser.add_argument('--dry-run', action='store_true', default=True,
                        help='Run in dry-run mode (no real trades)')
    parser.add_argument('--live', action='store_true',
                        help='Run in live mode (REAL TRADES)')
    parser.add_argument('--scan-interval', type=int, default=120,
                        help='Seconds between scans')
    parser.add_argument('--max-iterations', type=int, default=None,
                        help='Maximum iterations')
    parser.add_argument('--simulated-balance', type=float, default=None,
                        help='Simulated balance for dry-run testing')
    parser.add_argument('--min-price', type=float, default=None,
                        help='Minimum price threshold (default: 0.90)')
    parser.add_argument('--min-confidence', type=float, default=None,
                        help='Minimum confidence threshold (default: 0.75)')
    parser.add_argument('--btc-15min-only', action='store_true',
                        help='Restrict to 15-min BTC resolution markets (for testing)')
    parser.add_argument('--min-minutes-to-resolution', type=float, default=None,
                        help='Min minutes until resolution when using --btc-15min-only')
    parser.add_argument('--max-minutes-to-resolution', type=float, default=None,
                        help='Max minutes until resolution when using --btc-15min-only')
    parser.add_argument('--btc-scan-limit', type=int, default=None,
                        help='When using --btc-15min-only: number of markets to scan (default 500)')

    args = parser.parse_args()

    risk_config = RiskConfig()
    scalp_kwargs = {}
    if args.min_price is not None:
        scalp_kwargs['min_price_threshold'] = args.min_price
    if args.min_confidence is not None:
        scalp_kwargs['confidence_threshold'] = args.min_confidence
    if args.btc_15min_only:
        scalp_kwargs['btc_15min_only'] = True
        if args.min_minutes_to_resolution is not None:
            scalp_kwargs['min_minutes_to_resolution'] = args.min_minutes_to_resolution
        if args.max_minutes_to_resolution is not None:
            scalp_kwargs['max_minutes_to_resolution'] = args.max_minutes_to_resolution
        if args.btc_scan_limit is not None:
            scalp_kwargs['btc_15min_scan_limit'] = args.btc_scan_limit
    scalp_config = ScalpConfig(**scalp_kwargs)

    dry_run = not args.live

    scalper = ResolutionScalper(
        risk_config=risk_config,
        scalp_config=scalp_config,
        dry_run=dry_run,
        simulated_balance=args.simulated_balance
    )

    scalper.run(
        scan_interval=args.scan_interval,
        max_iterations=args.max_iterations
    )


if __name__ == "__main__":
    main()
