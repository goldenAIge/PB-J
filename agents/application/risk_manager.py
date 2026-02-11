"""
Portfolio Risk Manager for Polymarket Trading

Shared module used by both directional trader and resolution scalper.
Handles balance caching, position tracking, order book checks, market cooldowns,
and Kelly-based position sizing.
"""

import os
import time
import logging
from typing import Optional
from dataclasses import dataclass

import httpx
from pydantic import BaseModel, Field, validator

from agents.polymarket.polymarket import Polymarket

logger = logging.getLogger(__name__)


class RiskConfig(BaseModel):
    """Portfolio-level risk management configuration"""
    max_trade_percent: float = Field(default=0.05, description="Max % of wallet per trade (5%)")
    max_daily_loss_percent: float = Field(default=0.05, description="Stop trading if down 5% for the day")
    max_positions: int = Field(default=10, description="Max concurrent positions")
    max_exposure_percent: float = Field(default=0.60, description="Never deploy >60% of capital")
    min_liquidity_usd: float = Field(default=500, description="Min market liquidity in USD")
    min_volume_24h: float = Field(default=1000, description="Min 24h volume in USD")
    kelly_fraction: float = Field(default=0.25, description="Quarter-Kelly for safety")
    fee_rate: float = Field(default=0.02, description="Polymarket fee rate (~2%)")
    min_trade_size: float = Field(default=6.0, description="Minimum trade size in USDC")
    max_trade_size: float = Field(default=100.0, description="Maximum trade size in USDC")

    @validator('max_trade_percent', 'max_daily_loss_percent', 'max_exposure_percent', 'fee_rate')
    def validate_percentage(cls, v):
        if not 0 < v < 1:
            raise ValueError('Percentage must be between 0 and 1')
        return v


@dataclass
class OrderBookInfo:
    """Order book depth information"""
    best_price: float
    spread: float
    depth: float  # Total liquidity at best price level
    can_fill_amount: float  # Max amount fillable within spread tolerance


class PortfolioRiskManager:
    """
    Portfolio-level risk manager shared across all trading strategies.

    Features:
    - Cached balance (30s TTL) to prevent RPC hammering
    - Position tracking via Polymarket data API
    - Daily loss limit tracking
    - Market cooldowns after failures
    - Kelly-based position sizing
    - Order book depth checking
    """

    def __init__(self, polymarket: Polymarket, config: Optional[RiskConfig] = None):
        self.polymarket = polymarket
        self.config = config or RiskConfig()

        # Balance cache
        self._cached_balance: Optional[float] = None
        self._balance_cache_time: float = 0
        self._balance_cache_ttl: float = 30.0  # 30 second TTL

        # Daily P&L tracking
        self._daily_start_balance: Optional[float] = None
        self._daily_reset_date: Optional[str] = None

        # Market cooldowns: market_id -> cooldown_expiry_timestamp
        self._cooldowns: dict[str, float] = {}
        self._cooldown_duration: float = 600  # 10 minutes

        # Position tracking
        self._position_count: int = 0
        self._total_exposure: float = 0.0

    def get_balance(self, force_refresh: bool = False) -> float:
        """Get current USDC balance with 30s cache TTL."""
        now = time.time()
        if (
            not force_refresh
            and self._cached_balance is not None
            and (now - self._balance_cache_time) < self._balance_cache_ttl
        ):
            return self._cached_balance

        try:
            balance = self.polymarket.get_usdc_balance()
            self._cached_balance = balance
            self._balance_cache_time = now
            return balance
        except Exception as e:
            logger.error(f"Failed to get balance: {e}")
            if self._cached_balance is not None:
                return self._cached_balance
            return 0.0

    def get_available_capital(self) -> float:
        """Get capital available for new trades.

        Uses USDC cash balance directly. The max_exposure_percent limit
        only restricts NEW deployments — it won't block trading when
        pre-existing positions already exceed the cap.
        """
        balance = self.get_balance()  # USDC cash
        exposure = self._get_position_exposure()  # value of open positions
        total_portfolio = balance + exposure

        if total_portfolio <= 0:
            return 0.0

        # Cap new deployment at max_trade_percent of portfolio per trade,
        # but allow using available cash up to max_exposure_percent headroom
        max_deployed = total_portfolio * self.config.max_exposure_percent
        headroom = max(max_deployed - exposure, 0.0)

        # If existing positions already exceed the limit, still allow
        # deploying up to max_trade_percent of balance for new trades
        if headroom <= 0:
            available = balance * self.config.max_trade_percent
        else:
            available = min(balance, headroom)

        return max(available, 0.0)

    def _get_position_exposure(self) -> float:
        """Fetch open position exposure from Polymarket data API."""
        try:
            wallet = self.polymarket.get_address_for_private_key()
            response = httpx.get(
                f"https://data-api.polymarket.com/positions?user={wallet}",
                timeout=15
            )
            if response.status_code != 200:
                return self._total_exposure

            positions = response.json()
            total_exposure = 0.0
            position_count = 0

            for pos in positions:
                size = float(pos.get('size', 0))
                price = float(pos.get('avgPrice', 0))
                if size > 0:
                    total_exposure += size * price
                    position_count += 1

            self._total_exposure = total_exposure
            self._position_count = position_count
            return total_exposure

        except Exception as e:
            logger.debug(f"Failed to fetch positions: {e}")
            return self._total_exposure

    def can_open_position(self, trade_size: float) -> tuple[bool, str]:
        """
        Check if a new position can be opened.

        Returns (can_trade, reason).
        """
        balance = self.get_balance()

        # Check minimum balance
        if balance < self.config.min_trade_size:
            return False, f"Balance ${balance:.2f} below minimum ${self.config.min_trade_size}"

        # Check trade size vs balance
        if trade_size > balance:
            return False, f"Trade size ${trade_size:.2f} exceeds balance ${balance:.2f}"

        # Check max exposure (relative to total portfolio, not just cash)
        current_exposure = self._total_exposure
        total_portfolio = balance + current_exposure
        max_exposure = total_portfolio * self.config.max_exposure_percent

        # Only block if this NEW trade would push us over AND we're not already over
        # (pre-existing positions from before risk limits shouldn't block all trading)
        if current_exposure < max_exposure and (current_exposure + trade_size) > max_exposure:
            return False, f"Would exceed max exposure: ${current_exposure + trade_size:.2f} > ${max_exposure:.2f} ({self.config.max_exposure_percent:.0%})"

        # Check position count
        if self._position_count >= self.config.max_positions:
            return False, f"At max positions: {self._position_count}/{self.config.max_positions}"

        # Check daily loss limit
        if not self.check_daily_loss_limit():
            return False, "Daily loss limit exceeded"

        return True, "OK"

    def check_daily_loss_limit(self) -> bool:
        """Check if daily P&L exceeds the loss limit. Resets at midnight UTC."""
        import datetime
        today = datetime.datetime.utcnow().strftime("%Y-%m-%d")

        balance = self.get_balance()

        # Reset daily tracking at midnight
        if self._daily_reset_date != today:
            self._daily_start_balance = balance
            self._daily_reset_date = today
            return True

        if self._daily_start_balance is None or self._daily_start_balance == 0:
            self._daily_start_balance = balance
            return True

        daily_loss = (self._daily_start_balance - balance) / self._daily_start_balance
        if daily_loss >= self.config.max_daily_loss_percent:
            logger.error(
                f"Daily loss limit hit: {daily_loss:.2%} "
                f"(limit: {self.config.max_daily_loss_percent:.2%})"
            )
            return False

        return True

    def is_market_on_cooldown(self, market_id: str) -> bool:
        """Check if a market is on cooldown after a failure."""
        expiry = self._cooldowns.get(market_id)
        if expiry is None:
            return False
        if time.time() >= expiry:
            del self._cooldowns[market_id]
            return False
        remaining = expiry - time.time()
        logger.debug(f"Market {market_id} on cooldown for {remaining:.0f}s more")
        return True

    def record_failed_market(self, market_id: str) -> None:
        """Put a market on cooldown after a failure."""
        self._cooldowns[market_id] = time.time() + self._cooldown_duration
        logger.info(f"Market {market_id} on cooldown for {self._cooldown_duration}s")

    def calculate_position_size(self, edge: float, price: float) -> float:
        """
        Calculate position size using simplified Kelly criterion.

        size = kelly_fraction * (edge / (1 - price)) * available_capital
        Capped at max_trade_percent * balance.
        """
        if edge <= 0 or price <= 0 or price >= 1:
            return 0.0

        available = self.get_available_capital()
        balance = self.get_balance()

        # Kelly formula: fraction of bankroll to bet
        kelly_raw = edge / (1 - price)
        kelly_size = self.config.kelly_fraction * kelly_raw * available

        # Cap at max trade percent of total balance
        max_size = balance * self.config.max_trade_percent

        # Apply bounds
        size = min(kelly_size, max_size, self.config.max_trade_size)
        size = max(size, self.config.min_trade_size)

        # Final check: don't exceed available capital
        if size > available:
            size = available

        # If still below minimum, can't trade
        if size < self.config.min_trade_size:
            return 0.0

        return round(size, 2)

    def get_order_book_depth(
        self, token_id: str, side: str, target_price: float
    ) -> Optional[OrderBookInfo]:
        """
        Check order book depth for a token.

        Args:
            token_id: The CLOB token ID
            side: "BUY" or "SELL"
            target_price: The price we want to trade at

        Returns:
            OrderBookInfo with depth details, or None on error
        """
        try:
            orderbook = self.polymarket.get_orderbook(token_id)

            if side == "BUY":
                # We're buying, so we look at asks (sellers)
                orders = orderbook.asks if hasattr(orderbook, 'asks') else []
            else:
                # We're selling, so we look at bids (buyers)
                orders = orderbook.bids if hasattr(orderbook, 'bids') else []

            if not orders:
                return OrderBookInfo(
                    best_price=0.0, spread=1.0, depth=0.0, can_fill_amount=0.0
                )

            # Parse orders (they come as OrderSummary with price/size strings)
            parsed = []
            for order in orders:
                p = float(order.price)
                s = float(order.size)
                parsed.append((p, s))

            # Sort: asks ascending, bids descending
            if side == "BUY":
                parsed.sort(key=lambda x: x[0])
                best_price = parsed[0][0]
            else:
                parsed.sort(key=lambda x: x[0], reverse=True)
                best_price = parsed[0][0]

            # Calculate spread
            all_asks = orderbook.asks if hasattr(orderbook, 'asks') else []
            all_bids = orderbook.bids if hasattr(orderbook, 'bids') else []
            if all_asks and all_bids:
                best_ask = min(float(a.price) for a in all_asks)
                best_bid = max(float(b.price) for b in all_bids)
                spread = best_ask - best_bid
            else:
                spread = 1.0

            # Calculate depth: total size at best price level
            depth = sum(s for p, s in parsed if abs(p - best_price) < 0.005)

            # Calculate fillable amount within 2 cents of target
            tolerance = 0.02
            if side == "BUY":
                can_fill = sum(
                    s for p, s in parsed if p <= target_price + tolerance
                )
            else:
                can_fill = sum(
                    s for p, s in parsed if p >= target_price - tolerance
                )

            return OrderBookInfo(
                best_price=best_price,
                spread=spread,
                depth=depth,
                can_fill_amount=can_fill
            )

        except Exception as e:
            logger.debug(f"Failed to get order book for {token_id}: {e}")
            return None
