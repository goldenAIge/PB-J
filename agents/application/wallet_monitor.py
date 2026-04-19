"""
Wallet Stalking Strategy for Polymarket

Monitors top traders' wallets on Polymarket and generates copy-trade signals.
Polls the Data API for new activity, detects BUY trades, evaluates whether
to copy, and optionally executes via GTC limit orders (zero maker fees).

Primary targets:
- scottilicious (0x000d...758e): politics specialist, 86% WR, $1.5M P/L
- winner877 (0x85e5...6056): crypto specialist, 96.6% WR, $157K P/L

Usage:
    python -m agents.application.wallet_monitor --dry-run
    python -m agents.application.wallet_monitor --live --poll-interval 30
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
from dataclasses import dataclass, asdict, field

import requests
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from agents.polymarket.polymarket import Polymarket
from agents.application.risk_manager import RiskConfig, PortfolioRiskManager
from agents.connectors.telegram_alerts import TelegramAlerter

load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('wallet_monitor_trades.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

# Tracking file for positions opened by this strategy (not directional V2, etc.)
POSITIONS_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "wallet_monitor_positions.json")

SESSION = requests.Session()
SESSION.headers.update({
    "Accept": "application/json",
    "User-Agent": "PB&J-wallet-monitor/1.0",
})


# --- Tracked wallets ---

@dataclass
class TrackedWallet:
    """A wallet to monitor for copy-trade signals."""
    label: str
    address: str
    specialty: str  # "politics", "crypto", "sports", etc.
    win_rate: float  # historical win rate (0-1)
    avg_trade_usdc: float  # average trade size in USDC
    copy_weight: float = 1.0  # multiplier for position sizing (0.5 = half size)
    categories: list[str] = field(default_factory=list)  # which categories to copy


DEFAULT_WALLETS = [
    TrackedWallet(
        label="scottilicious",
        address="0x000d257d2dc7616feaef4ae0f14600fdf50a758e",
        specialty="politics",
        win_rate=0.86,
        avg_trade_usdc=2310,
        copy_weight=1.0,
        categories=["politics", "tech"],  # 86% politics, 95.5% tech
    ),
    TrackedWallet(
        label="winner877",
        address="0x85e5669beee6b80d887493e724987dabc5f56056",
        specialty="crypto",
        win_rate=0.949,
        avg_trade_usdc=778,
        copy_weight=1.0,
        categories=["crypto", "sports"],  # 94.9% crypto, 85.9% sports
    ),
]


class WalletMonitorConfig(BaseModel):
    """Configuration for wallet copy-trade monitoring."""
    poll_interval: float = Field(default=30.0, description="Seconds between activity polls")
    # Copy sizing
    copy_fraction: float = Field(default=0.02, description="Copy at 2% of our balance per signal")
    max_copy_size: float = Field(default=15.0, description="Max USDC per copy trade")
    min_copy_size: float = Field(default=1.0, description="Min USDC per copy trade")
    # Filters
    min_whale_trade_usdc: float = Field(default=50.0, description="Ignore whale trades smaller than this")
    max_entry_price: float = Field(default=0.92, description="Don't copy if token price > this (too little upside)")
    min_entry_price: float = Field(default=0.05, description="Don't copy if token price < this (too speculative)")
    min_book_depth_shares: float = Field(default=50.0, description="Min order book depth in shares")
    max_spread: float = Field(default=0.05, description="Max bid-ask spread")
    # Dedup
    cooldown_per_market_secs: float = Field(default=3600.0, description="Don't re-copy same market within 1 hour")
    max_copies_per_cycle: int = Field(default=2, description="Max copy trades per poll cycle")
    # Staleness
    max_trade_age_secs: float = Field(default=300.0, description="Ignore trades older than 5 minutes")
    # Category filter
    respect_wallet_categories: bool = Field(default=True, description="Only copy in wallet's strong categories")


@dataclass
class WhaleTrade:
    """A detected trade from a tracked wallet."""
    wallet_label: str
    wallet_address: str
    timestamp: int  # unix epoch
    side: str  # "BUY" or "SELL"
    market_title: str
    outcome: str  # "Yes" / "No" / outcome name
    price: float
    size_shares: float
    usdc_size: float
    token_id: str
    condition_id: str
    event_slug: str
    asset_id: str


@dataclass
class CopySignal:
    """A validated copy-trade signal ready for execution."""
    whale_trade: WhaleTrade
    our_side: str  # BUY side token_id
    our_price: float  # best ask or whale price
    our_size_usdc: float
    our_shares: float
    reason: str
    category: str


@dataclass
class ExitSignal:
    """A validated exit signal triggered by a whale SELL."""
    whale_trade: WhaleTrade
    our_token_id: str
    our_shares: float  # shares we hold
    sell_price: float  # best bid or whale price
    reason: str


@dataclass
class CopyTradeResult:
    """Result of a copy-trade execution."""
    signal: CopySignal
    status: str  # "DRY_RUN", "FILLED", "TIMEOUT", "FAILED"
    order_id: Optional[str] = None
    error: Optional[str] = None
    timestamp: str = ""


@dataclass
class ExitTradeResult:
    """Result of an exit trade execution."""
    signal: ExitSignal
    status: str  # "DRY_RUN", "PLACED", "FAILED"
    order_id: Optional[str] = None
    error: Optional[str] = None
    timestamp: str = ""


class WalletMonitor:
    """
    Monitors tracked wallets and generates copy-trade signals.

    Architecture:
    1. Poll /activity for each tracked wallet
    2. Detect new BUY trades since last poll
    3. Filter: category, size, price, book depth
    4. Size the copy trade (fraction of balance, capped)
    5. Execute via GTC limit order or dry-run
    6. Alert via Telegram
    """

    def __init__(
        self,
        wallets: Optional[list[TrackedWallet]] = None,
        config: Optional[WalletMonitorConfig] = None,
        risk_config: Optional[RiskConfig] = None,
        dry_run: bool = True,
        simulated_balance: Optional[float] = None,
    ):
        self.wallets = wallets or DEFAULT_WALLETS
        self.config = config or WalletMonitorConfig()
        self.dry_run = dry_run

        # Polymarket client + risk manager
        self.polymarket = Polymarket()
        self.risk_manager = PortfolioRiskManager(
            self.polymarket,
            risk_config or RiskConfig(
                min_trade_size=self.config.min_copy_size,
                max_trade_size=self.config.max_copy_size,
            ),
        )

        # Simulated balance for dry run
        self._simulated_balance = simulated_balance

        # State: last-seen activity timestamp per wallet
        self._last_seen_ts: dict[str, int] = {}
        # Cooldown: market_id -> expiry timestamp
        self._market_cooldowns: dict[str, float] = {}
        # Stats
        self.total_signals = 0
        self.total_copies = 0
        self.total_skipped = 0
        self.total_exits = 0
        self.copy_history: list[CopyTradeResult] = []
        self.exit_history: list[ExitTradeResult] = []

        # Sell approval flag — only check/set once per session
        self._sell_approval_done = False

        # Telegram
        self.alerter = TelegramAlerter()

        # Position tracking — only exit positions we opened
        self._tracked_positions = self._load_tracked_positions()

    # --- Position tracking (persistence) ---

    def _load_tracked_positions(self) -> dict[str, dict]:
        """Load tracked positions from disk.

        Returns dict keyed by token_id with metadata:
            {token_id: {"market_title": str, "whale_label": str, "timestamp": str, "shares": float, "price": float}}
        """
        try:
            with open(POSITIONS_FILE, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_tracked_positions(self):
        """Persist tracked positions to disk."""
        try:
            os.makedirs(os.path.dirname(POSITIONS_FILE), exist_ok=True)
            with open(POSITIONS_FILE, "w") as f:
                json.dump(self._tracked_positions, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save tracked positions: {e}")

    def _track_position(self, token_id: str, market_title: str, whale_label: str, shares: float, price: float):
        """Record a position opened by the wallet monitor."""
        self._tracked_positions[token_id] = {
            "market_title": market_title,
            "whale_label": whale_label,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "shares": shares,
            "price": price,
        }
        self._save_tracked_positions()
        logger.info(f"Tracked position: {market_title[:50]} ({token_id[:20]}...)")

    def _untrack_position(self, token_id: str):
        """Remove a position from tracking after exit."""
        if token_id in self._tracked_positions:
            title = self._tracked_positions[token_id].get("market_title", "")
            del self._tracked_positions[token_id]
            self._save_tracked_positions()
            logger.info(f"Untracked position: {title[:50]} ({token_id[:20]}...)")

    # --- Data API helpers ---

    def _fetch_json(self, url: str, params: dict = None, timeout: float = 30.0):
        try:
            r = SESSION.get(url, params=params or {}, timeout=timeout)
            if r.status_code != 200:
                logger.warning(f"API {r.status_code}: {url}")
                return None
            return r.json()
        except Exception as e:
            logger.error(f"Request failed: {url} — {e}")
            return None

    def _fetch_recent_activity(self, address: str, limit: int = 50) -> list[dict]:
        """Fetch most recent activity for a wallet."""
        data = self._fetch_json(
            f"{DATA_API}/activity",
            params={"user": address, "limit": limit, "offset": 0},
        )
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            items = (
                data.get("data")
                or data.get("activities")
                or data.get("results")
                or data.get("items")
            )
            if isinstance(items, list):
                return items
        return []

    def _fetch_market_by_condition(self, condition_id: str) -> Optional[dict]:
        """Fetch market data by condition ID from Gamma."""
        data = self._fetch_json(
            f"{GAMMA_API}/markets",
            params={"condition_id": condition_id},
        )
        if isinstance(data, list) and data:
            return data[0]
        return None

    def _classify_market(self, event_slug: str, title: str) -> str:
        """Simple keyword-based category classification."""
        text = f"{event_slug or ''} {title or ''}".lower()
        checks = [
            ("crypto", ("bitcoin", "ethereum", "crypto", "token", "fdv", "airdrop", "solana", "defi", "btc", "eth")),
            ("sports", ("nba", "nfl", "mlb", "nhl", "ufc", "dota", "lol", "soccer", "fc ", "vs.")),
            ("tech", ("gpt", "openai", "ai ", "tech", "apple", "google", "tesla", "aapl")),
            ("politics", ("trump", "biden", "election", "senate", "congress", "president", "gop",
                          "democrat", "regime", "ceasefire", "iran", "ukraine", "war", "military",
                          "israel", "yemen", "strike", "nato", "nuclear", "sanction", "pardon",
                          "governor", "nominee", "primary", "midterm", "resign", "netanyahu",
                          "zelenskyy", "pahlavi", "khamenei", "siege", "chile", "venezuela",
                          "brazil", "board of peace", "fed ", "tariff", "diplomacy",
                          "invasion", "offensive", "embassy", "geopolit")),
            ("culture", ("oscar", "grammy", "movie", "celebrity", "album")),
        ]
        for bucket, keys in checks:
            if any(k in text for k in keys):
                return bucket
        return "other"

    # --- Polling & detection ---

    def poll_wallet(self, wallet: TrackedWallet) -> tuple[list[WhaleTrade], list[WhaleTrade]]:
        """Poll a wallet's activity and return new BUY and SELL trades since last check.

        Returns:
            (buy_trades, sell_trades)
        """
        activity = self._fetch_recent_activity(wallet.address)
        if not activity:
            return [], []

        last_ts = self._last_seen_ts.get(wallet.address, 0)
        buy_trades: list[WhaleTrade] = []
        sell_trades: list[WhaleTrade] = []
        max_ts = last_ts

        now_epoch = int(time.time())

        for row in activity:
            if row.get("type") != "TRADE":
                continue

            ts = int(row.get("timestamp") or 0)
            if ts <= last_ts:
                continue

            max_ts = max(max_ts, ts)

            side = (row.get("side") or "").upper()
            if side not in ("BUY", "SELL"):
                continue

            # Staleness check
            age = now_epoch - ts
            if age > self.config.max_trade_age_secs:
                continue

            # Parse trade details
            usdc_size = 0.0
            raw_usdc = row.get("usdcSize")
            if raw_usdc is not None:
                try:
                    usdc_size = float(raw_usdc)
                except (TypeError, ValueError):
                    pass
            if usdc_size == 0:
                try:
                    usdc_size = float(row.get("size") or 0) * float(row.get("price") or 0)
                except (TypeError, ValueError):
                    pass

            trade = WhaleTrade(
                wallet_label=wallet.label,
                wallet_address=wallet.address,
                timestamp=ts,
                side=side,
                market_title=(row.get("title") or "(unknown)")[:100],
                outcome=row.get("outcome") or row.get("outcomeSide") or "",
                price=float(row.get("price") or 0),
                size_shares=float(row.get("size") or 0),
                usdc_size=usdc_size,
                token_id=row.get("asset") or "",
                condition_id=row.get("conditionId") or "",
                event_slug=row.get("eventSlug") or "",
                asset_id=row.get("asset") or "",
            )
            if side == "BUY":
                buy_trades.append(trade)
            else:
                sell_trades.append(trade)

        # Update last-seen timestamp
        if max_ts > last_ts:
            self._last_seen_ts[wallet.address] = max_ts

        return buy_trades, sell_trades

    # --- Signal evaluation ---

    def evaluate_trade(self, trade: WhaleTrade, wallet: TrackedWallet) -> Optional[CopySignal]:
        """Evaluate a whale trade and return a CopySignal if worth copying."""

        # 1. Size filter
        if trade.usdc_size < self.config.min_whale_trade_usdc:
            logger.debug(f"Skip {trade.wallet_label}: ${trade.usdc_size:.0f} < min ${self.config.min_whale_trade_usdc}")
            return None

        # 2. Category filter
        category = self._classify_market(trade.event_slug, trade.market_title)
        if self.config.respect_wallet_categories and wallet.categories:
            if category not in wallet.categories:
                logger.info(
                    f"Skip {trade.wallet_label} trade in '{category}' "
                    f"(only copying {wallet.categories}): {trade.market_title[:50]}"
                )
                return None

        # 3. Price filter
        if trade.price > self.config.max_entry_price:
            logger.debug(f"Skip: price ${trade.price:.2f} > max ${self.config.max_entry_price}")
            return None
        if trade.price < self.config.min_entry_price:
            logger.debug(f"Skip: price ${trade.price:.2f} < min ${self.config.min_entry_price}")
            return None

        # 4. Market cooldown
        cd_key = f"{trade.condition_id}:{trade.token_id}"
        cd_expiry = self._market_cooldowns.get(cd_key, 0)
        if time.time() < cd_expiry:
            logger.debug(f"Skip: market on cooldown — {trade.market_title[:40]}")
            return None

        # 5. Order book check
        if trade.token_id:
            ob_info = self.risk_manager.get_order_book_depth(
                trade.token_id, "BUY", trade.price
            )
            if ob_info:
                if ob_info.spread > self.config.max_spread:
                    logger.info(f"Skip: spread {ob_info.spread:.3f} > max {self.config.max_spread}")
                    return None
                if ob_info.depth < self.config.min_book_depth_shares:
                    logger.info(f"Skip: depth {ob_info.depth:.0f} < min {self.config.min_book_depth_shares}")
                    return None
                # Use best ask for our entry
                entry_price = ob_info.best_price
            else:
                entry_price = trade.price
        else:
            logger.warning(f"Skip: no token_id for {trade.market_title[:40]}")
            return None

        # Re-check price bounds after book lookup
        if entry_price > self.config.max_entry_price:
            logger.debug(f"Skip: book price ${entry_price:.2f} > max")
            return None

        # 6. Position sizing
        balance = self._get_balance()
        copy_size = balance * self.config.copy_fraction * wallet.copy_weight
        copy_size = min(copy_size, self.config.max_copy_size)
        copy_size = max(copy_size, self.config.min_copy_size)

        if copy_size > balance:
            logger.warning(f"Insufficient balance: ${balance:.2f}")
            return None

        shares = copy_size / entry_price if entry_price > 0 else 0
        if shares <= 0:
            return None

        # Build signal
        profit_pct = (1.0 - entry_price) / entry_price
        reason = (
            f"Copy {trade.wallet_label} ({wallet.win_rate:.0%} WR in {category}) | "
            f"whale: {trade.size_shares:.0f} shares @ ${trade.price:.2f} (${trade.usdc_size:.0f}) | "
            f"our entry: ${entry_price:.2f}, profit potential: {profit_pct:.1%}"
        )

        return CopySignal(
            whale_trade=trade,
            our_side=trade.token_id,
            our_price=entry_price,
            our_size_usdc=round(copy_size, 2),
            our_shares=round(shares, 2),
            reason=reason,
            category=category,
        )

    # --- Execution ---

    def execute_copy(self, signal: CopySignal) -> CopyTradeResult:
        """Execute a copy trade (GTC limit order or dry-run)."""
        result = CopyTradeResult(
            signal=signal,
            status="PENDING",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        whale = signal.whale_trade
        logger.info(f"\n{'='*60}")
        logger.info(f"COPY TRADE SIGNAL — {whale.wallet_label}")
        logger.info(f"Market: {whale.market_title}")
        logger.info(f"Whale: {whale.side} {whale.size_shares:.0f} shares @ ${whale.price:.2f} (${whale.usdc_size:.0f})")
        logger.info(f"Our order: BUY {signal.our_shares:.2f} shares @ ${signal.our_price:.2f} (${signal.our_size_usdc:.2f})")
        logger.info(f"Category: {signal.category} | {signal.reason}")
        logger.info(f"{'='*60}\n")

        if self.dry_run:
            result.status = "DRY_RUN"
            logger.info("[DRY RUN] Copy trade simulated — not placed on chain")
            if self._simulated_balance is not None:
                self._simulated_balance -= signal.our_size_usdc
        else:
            # Risk check
            can_trade, reason = self.risk_manager.can_open_position(signal.our_size_usdc)
            if not can_trade:
                logger.warning(f"Risk check failed: {reason}")
                result.status = "FAILED"
                result.error = reason
                return result

            try:
                logger.warning(">>> EXECUTING REAL COPY TRADE — FUNDS WILL BE USED <<<")
                order_response = self.polymarket.execute_limit_buy(
                    token_id=signal.our_side,
                    price=signal.our_price,
                    size=signal.our_shares,
                )

                order_id = None
                if isinstance(order_response, dict):
                    order_id = order_response.get("orderID") or order_response.get("id")
                elif isinstance(order_response, str):
                    order_id = order_response

                result.order_id = str(order_id) if order_id else None
                result.status = "PLACED"
                logger.info(f"Order placed: {order_id}")

                # Refresh balance cache
                self.risk_manager.get_balance(force_refresh=True)

            except Exception as e:
                logger.error(f"Copy trade failed: {e}")
                result.status = "FAILED"
                result.error = str(e)

        # Track position for exit matching (only our own positions)
        if result.status in ("DRY_RUN", "PLACED"):
            self._track_position(
                token_id=signal.our_side,
                market_title=whale.market_title,
                whale_label=whale.wallet_label,
                shares=signal.our_shares,
                price=signal.our_price,
            )

        # Set cooldown on this market
        cd_key = f"{whale.condition_id}:{whale.token_id}"
        self._market_cooldowns[cd_key] = time.time() + self.config.cooldown_per_market_secs

        self.copy_history.append(result)
        return result

    # --- Exit logic (SELL signal handling) ---

    def _fetch_our_positions(self) -> list[dict]:
        """Fetch positions opened by the wallet monitor (not other strategies).

        Queries the Data API for all open positions, then filters to only those
        whose token_id is in our tracked positions file.
        """
        if not self._tracked_positions:
            return []

        try:
            wallet = self.polymarket.get_address_for_private_key()
            r = SESSION.get(
                f"{DATA_API}/positions",
                params={"user": wallet},
                timeout=15,
            )
            if r.status_code != 200:
                logger.warning(f"Failed to fetch positions: HTTP {r.status_code}")
                return []
            positions = r.json()
            # Only return positions that the wallet monitor opened
            return [
                p for p in positions
                if float(p.get("size", 0)) > 0
                and p.get("asset", "") in self._tracked_positions
            ]
        except Exception as e:
            logger.error(f"Failed to fetch positions: {e}")
            return []

    def evaluate_exit(self, sell_trade: WhaleTrade, our_positions: list[dict]) -> Optional[ExitSignal]:
        """Check if a whale SELL matches a position we hold, and build an exit signal."""
        for pos in our_positions:
            pos_asset = pos.get("asset", "")
            if pos_asset != sell_trade.token_id:
                continue

            our_shares = float(pos.get("size", 0))
            if our_shares <= 0:
                continue

            # Match found — whale is selling a token we hold
            sell_price = sell_trade.price
            reason = (
                f"Exit copy {sell_trade.wallet_label} SELL | "
                f"whale sold {sell_trade.size_shares:.0f}sh @ ${sell_trade.price:.2f} | "
                f"we hold {our_shares:.2f}sh"
            )

            return ExitSignal(
                whale_trade=sell_trade,
                our_token_id=pos_asset,
                our_shares=our_shares,
                sell_price=sell_price,
                reason=reason,
            )

        return None

    def execute_exit(self, signal: ExitSignal) -> ExitTradeResult:
        """Execute an exit trade (GTC limit sell or dry-run)."""
        result = ExitTradeResult(
            signal=signal,
            status="PENDING",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        whale = signal.whale_trade
        logger.info(f"\n{'='*60}")
        logger.info(f"EXIT SIGNAL — {whale.wallet_label} SOLD")
        logger.info(f"Market: {whale.market_title}")
        logger.info(f"Whale: SELL {whale.size_shares:.0f} shares @ ${whale.price:.2f}")
        logger.info(f"Our exit: SELL {signal.our_shares:.2f} shares @ ${signal.sell_price:.2f}")
        logger.info(f"Reason: {signal.reason}")
        logger.info(f"{'='*60}\n")

        if self.dry_run:
            result.status = "DRY_RUN"
            logger.info("[DRY RUN] Exit trade simulated — not placed on chain")
        else:
            try:
                # Ensure CTF approval for sell (once per session)
                if not self._sell_approval_done:
                    logger.info("Checking CTF sell approvals...")
                    self.polymarket.ensure_sell_approval()
                    self._sell_approval_done = True

                logger.warning(">>> EXECUTING REAL EXIT TRADE — SELLING POSITION <<<")
                order_response = self.polymarket.execute_limit_sell(
                    token_id=signal.our_token_id,
                    price=signal.sell_price,
                    size=signal.our_shares,
                )

                order_id = None
                if isinstance(order_response, dict):
                    order_id = order_response.get("orderID") or order_response.get("id")
                elif isinstance(order_response, str):
                    order_id = order_response

                result.order_id = str(order_id) if order_id else None
                result.status = "PLACED"
                logger.info(f"Exit order placed: {order_id}")

                self.risk_manager.get_balance(force_refresh=True)

            except Exception as e:
                logger.error(f"Exit trade failed: {e}")
                result.status = "FAILED"
                result.error = str(e)

        # Untrack position after successful exit
        if result.status in ("DRY_RUN", "PLACED"):
            self._untrack_position(signal.our_token_id)

        return result

    async def _send_exit_alert(self, result: ExitTradeResult):
        """Send Telegram alert for an exit trade."""
        whale = result.signal.whale_trade
        signal = result.signal
        status_emoji = {
            "DRY_RUN": "🧪", "PLACED": "✅", "FAILED": "❌",
        }.get(result.status, "❓")

        msg = (
            f"{status_emoji} <b>EXIT TRADE — {whale.wallet_label} SOLD</b>\n\n"
            f"📊 <b>{whale.market_title}</b>\n"
            f"🐋 Whale: SELL {whale.size_shares:.0f}sh @ ${whale.price:.2f}\n"
            f"📋 Our exit: SELL {signal.our_shares:.2f}sh @ ${signal.sell_price:.2f}\n"
            f"📈 Status: {result.status}\n"
        )
        if result.order_id:
            msg += f"🔗 Order: <code>{result.order_id}</code>\n"
        if result.error:
            msg += f"⚠️ Error: {result.error}\n"

        try:
            await self.alerter.send_message(msg)
        except Exception as e:
            logger.debug(f"Telegram exit alert failed: {e}")

    # --- Telegram alerts ---

    async def _send_startup_alert(self):
        """Send Telegram startup notification."""
        wallet_lines = "\n".join(
            f"  {w.label} — {w.specialty}, {w.win_rate:.0%} WR"
            for w in self.wallets
        )
        msg = (
            f"🔍 <b>WALLET STALKER STARTED</b>\n\n"
            f"📡 Tracking {len(self.wallets)} wallets:\n"
            f"<code>{wallet_lines}</code>\n\n"
            f"⚙️ Poll: {self.config.poll_interval}s | "
            f"Max copy: ${self.config.max_copy_size} | "
            f"Mode: {'DRY RUN' if self.dry_run else 'LIVE'}\n"
            f"💰 Balance: ${self._get_balance():.2f}"
        )
        try:
            await self.alerter.send_message(msg)
        except Exception as e:
            logger.debug(f"Telegram startup alert failed: {e}")

    async def _send_alert(self, result: CopyTradeResult):
        """Send Telegram alert for a copy trade (HTML format)."""
        whale = result.signal.whale_trade
        signal = result.signal
        status_emoji = {
            "DRY_RUN": "🧪", "PLACED": "✅", "FILLED": "💰",
            "FAILED": "❌", "TIMEOUT": "⏰",
        }.get(result.status, "❓")

        msg = (
            f"{status_emoji} <b>COPY TRADE — {whale.wallet_label}</b>\n\n"
            f"📊 <b>{whale.market_title}</b>\n"
            f"🐋 Whale: {whale.side} {whale.size_shares:.0f}sh @ ${whale.price:.2f} (${whale.usdc_size:.0f})\n"
            f"📋 Our: BUY {signal.our_shares:.0f}sh @ ${signal.our_price:.2f} (${signal.our_size_usdc:.2f})\n"
            f"🏷 Category: {signal.category}\n"
            f"📈 Status: {result.status}\n"
        )
        if result.order_id:
            msg += f"🔗 Order: <code>{result.order_id}</code>\n"
        if result.error:
            msg += f"⚠️ Error: {result.error}\n"

        try:
            await self.alerter.send_message(msg)
        except Exception as e:
            logger.debug(f"Telegram alert failed: {e}")

    # --- Main loop ---

    def _get_balance(self) -> float:
        if self._simulated_balance is not None:
            return self._simulated_balance
        return self.risk_manager.get_balance()

    def _seed_last_seen(self):
        """
        On startup, fetch the latest activity timestamp for each wallet
        so we don't replay old trades on first poll.
        """
        for wallet in self.wallets:
            activity = self._fetch_recent_activity(wallet.address, limit=5)
            if activity:
                ts_list = [int(a.get("timestamp") or 0) for a in activity if a.get("timestamp")]
                if ts_list:
                    self._last_seen_ts[wallet.address] = max(ts_list)
                    logger.info(
                        f"Seeded {wallet.label}: last activity "
                        f"{datetime.fromtimestamp(max(ts_list), tz=timezone.utc).strftime('%H:%M:%S UTC')}"
                    )

    def _print_status(self, iteration: int):
        balance = self._get_balance()
        logger.info(
            f"[Iter {iteration}] Balance: ${balance:.2f} | "
            f"Signals: {self.total_signals} | Copies: {self.total_copies} | "
            f"Exits: {self.total_exits} | Skipped: {self.total_skipped} | "
            f"Monitoring: {', '.join(w.label for w in self.wallets)}"
        )

    async def run(
        self,
        poll_interval: Optional[float] = None,
        max_iterations: Optional[int] = None,
    ):
        """Main async monitoring loop."""
        interval = poll_interval or self.config.poll_interval

        logger.info(f"\n{'='*60}")
        logger.info("STARTING WALLET STALKING STRATEGY")
        logger.info(f"Tracking {len(self.wallets)} wallets:")
        for w in self.wallets:
            logger.info(f"  {w.label} ({w.address[:10]}...) — {w.specialty}, {w.win_rate:.0%} WR, categories={w.categories}")
        logger.info(f"Poll interval: {interval}s")
        logger.info(f"Copy sizing: {self.config.copy_fraction:.0%} of balance, max ${self.config.max_copy_size}")
        logger.info(f"Mode: {'DRY RUN' if self.dry_run else 'LIVE'}")
        logger.info(f"{'='*60}\n")

        # Seed last-seen timestamps to avoid replaying old trades
        self._seed_last_seen()

        # Send Telegram startup notification
        await self._send_startup_alert()

        iteration = 0
        while True:
            iteration += 1
            if max_iterations and iteration > max_iterations:
                logger.info(f"Reached max iterations ({max_iterations}), stopping.")
                break

            self._print_status(iteration)
            copies_this_cycle = 0

            # Fetch our positions once per cycle (used for exit matching)
            our_positions = None  # lazy-loaded if we see SELL trades

            for wallet in self.wallets:
                try:
                    buy_trades, sell_trades = self.poll_wallet(wallet)

                    if buy_trades:
                        logger.info(
                            f"  {wallet.label}: {len(buy_trades)} new BUY trade(s) detected"
                        )

                    if sell_trades:
                        logger.info(
                            f"  {wallet.label}: {len(sell_trades)} new SELL trade(s) detected"
                        )

                    # --- Handle BUY trades (copy) ---
                    for trade in buy_trades:
                        self.total_signals += 1

                        if copies_this_cycle >= self.config.max_copies_per_cycle:
                            logger.info("Max copies per cycle reached, deferring remaining signals")
                            break

                        signal = self.evaluate_trade(trade, wallet)
                        if signal is None:
                            self.total_skipped += 1
                            continue

                        result = self.execute_copy(signal)
                        if result.status in ("DRY_RUN", "PLACED", "FILLED"):
                            self.total_copies += 1
                            copies_this_cycle += 1

                        # Send Telegram alert
                        try:
                            await self._send_alert(result)
                        except Exception:
                            pass

                    # --- Handle SELL trades (exit) ---
                    for sell_trade in sell_trades:
                        # Lazy-load our positions on first SELL trade
                        if our_positions is None:
                            our_positions = self._fetch_our_positions()

                        exit_signal = self.evaluate_exit(sell_trade, our_positions)
                        if exit_signal is None:
                            logger.debug(
                                f"  {wallet.label} SELL ignored (no matching position): "
                                f"{sell_trade.market_title[:50]}"
                            )
                            continue

                        exit_result = self.execute_exit(exit_signal)
                        if exit_result.status in ("DRY_RUN", "PLACED"):
                            self.total_exits += 1
                            self.exit_history.append(exit_result)
                            # Invalidate position cache so next SELL re-fetches
                            our_positions = None

                        try:
                            await self._send_exit_alert(exit_result)
                        except Exception:
                            pass

                except Exception as e:
                    logger.error(f"Error polling {wallet.label}: {e}")

            # Wait for next poll
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                logger.info("Monitor cancelled, shutting down.")
                break

        # Final summary
        self._print_summary()

    def _print_summary(self):
        logger.info(f"\n{'='*60}")
        logger.info("WALLET STALKER SESSION SUMMARY")
        logger.info(f"Total signals detected: {self.total_signals}")
        logger.info(f"Total copy trades: {self.total_copies}")
        logger.info(f"Total exit trades: {self.total_exits}")
        logger.info(f"Total skipped: {self.total_skipped}")
        if self.copy_history:
            logger.info("\nCopy trade log:")
            for r in self.copy_history:
                w = r.signal.whale_trade
                logger.info(
                    f"  [{r.status}] {w.wallet_label}: {w.market_title[:50]} | "
                    f"${r.signal.our_size_usdc:.2f} @ ${r.signal.our_price:.2f}"
                )
        if self.exit_history:
            logger.info("\nExit trade log:")
            for r in self.exit_history:
                w = r.signal.whale_trade
                logger.info(
                    f"  [{r.status}] {w.wallet_label} SELL: {w.market_title[:50]} | "
                    f"{r.signal.our_shares:.2f}sh @ ${r.signal.sell_price:.2f}"
                )
        logger.info(f"Final balance: ${self._get_balance():.2f}")
        logger.info(f"{'='*60}\n")


# --- CLI entrypoint ---

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Polymarket Wallet Stalking Strategy")
    ap.add_argument("--dry-run", action="store_true", default=True, help="Simulate trades (default)")
    ap.add_argument("--live", action="store_true", help="Execute real trades")
    ap.add_argument("--poll-interval", type=float, default=30.0, help="Seconds between polls (default: 30)")
    ap.add_argument("--max-iterations", type=int, default=None, help="Max poll iterations")
    ap.add_argument("--simulated-balance", type=float, default=None, help="Simulated balance for dry-run")
    ap.add_argument("--max-copy-size", type=float, default=15.0, help="Max USDC per copy trade")
    ap.add_argument("--copy-fraction", type=float, default=0.02, help="Fraction of balance per copy (default: 2%%)")
    ap.add_argument("--min-whale-trade", type=float, default=50.0, help="Min whale trade USDC to copy")
    ap.add_argument("--max-entry-price", type=float, default=0.92, help="Max entry price to copy")
    return ap.parse_args()


def main():
    args = parse_args()
    dry_run = not args.live

    config = WalletMonitorConfig(
        poll_interval=args.poll_interval,
        max_copy_size=args.max_copy_size,
        copy_fraction=args.copy_fraction,
        min_whale_trade_usdc=args.min_whale_trade,
        max_entry_price=args.max_entry_price,
    )

    monitor = WalletMonitor(
        config=config,
        dry_run=dry_run,
        simulated_balance=args.simulated_balance,
    )

    asyncio.run(monitor.run(
        poll_interval=args.poll_interval,
        max_iterations=args.max_iterations,
    ))


if __name__ == "__main__":
    main()
