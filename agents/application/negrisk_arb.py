"""
NegRisk Multi-Outcome Arbitrage Scanner for Polymarket

Strategy: Buy 1 share of every outcome in a multi-outcome negRisk market
when sum(best_ask_YES_prices) < $1.00. At resolution, exactly one outcome
wins and pays $1.00. Profit = $1.00 - total_cost.

Zero prediction risk — pure math arbitrage.

Usage:
    python -m agents.application.negrisk_arb --dry-run --simulated-balance 250
    python -m agents.application.negrisk_arb --dry-run --max-iterations 3
"""

import os
import sys
import time
import asyncio
import logging
import argparse
from datetime import datetime, timezone
from typing import Optional
from dataclasses import dataclass, field, asdict

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from agents.polymarket.polymarket import Polymarket
from agents.polymarket.gamma import GammaMarketClient
from agents.connectors.telegram_alerts import TelegramAlerter

load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('negrisk_arb_trades.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


class NegRiskArbConfig(BaseModel):
    """Configuration for negRisk multi-outcome arbitrage"""
    # Arb detection
    min_spread: float = Field(default=0.03, description="Min profit per share ($0.03 = 3%)")
    max_outcome_price: float = Field(default=0.95, description="Skip outcomes priced > $0.95")
    min_outcomes: int = Field(default=3, description="Min outcomes per event")
    max_days_to_resolution: float = Field(default=14.0, description="Max days until event resolves")

    # Position sizing
    max_position_pct: float = Field(default=0.15, description="15% of balance per arb")
    max_position_size: float = Field(default=30.0, description="Max total per arb ($30)")
    min_leg_size: float = Field(default=1.0, description="Min shares per outcome")

    # Execution
    scan_interval: float = Field(default=30.0, description="Seconds between scans")
    order_timeout: float = Field(default=10.0, description="Seconds to wait for fills")
    cancel_on_partial: bool = Field(default=True, description="Cancel unfilled legs after timeout")
    price_buffer: float = Field(default=0.005, description="Pay up to 0.5c above best ask")

    # Monitoring
    status_interval: float = Field(default=300.0, description="Status log every 5 min")


@dataclass
class ArbOpportunity:
    """A detected arbitrage opportunity"""
    event_id: str
    event_title: str
    event_slug: str
    num_outcomes: int
    total_ask_price: float  # Sum of best asks
    spread: float  # 1.0 - total_ask_price
    max_shares: float  # Max shares buyable (limited by shallowest book)
    suggested_size: float  # Shares to buy
    outcomes: list  # List of outcome dicts with best_ask info


@dataclass
class ArbLeg:
    """A single leg of an executed arb"""
    label: str
    token_id: str
    condition_id: str
    market_id: str
    price: float
    shares: float
    cost: float
    order_id: Optional[str] = None
    filled: bool = False
    fill_price: Optional[float] = None


@dataclass
class ArbPosition:
    """A complete arb position (all legs)"""
    event_id: str
    event_title: str
    event_slug: str
    timestamp: str
    legs: list[ArbLeg] = field(default_factory=list)
    total_cost: float = 0.0
    shares: float = 0.0
    expected_profit: float = 0.0
    status: str = "open"  # "open", "partial", "won", "lost"
    actual_profit: Optional[float] = None
    is_dry_run: bool = True


class NegRiskArbBot:
    """
    NegRisk Multi-Outcome Arbitrage Bot.

    Scans multi-outcome negRisk events for arbitrage opportunities where
    sum(best_ask) < $1.00, then buys equal shares of every outcome.
    """

    def __init__(
        self,
        config: Optional[NegRiskArbConfig] = None,
        dry_run: bool = True,
        simulated_balance: Optional[float] = None,
    ):
        self.config = config or NegRiskArbConfig()
        self.dry_run = dry_run
        self.simulated_balance = simulated_balance

        # Clients
        self.poly: Optional[Polymarket] = None
        self.gamma = GammaMarketClient()
        self.alerter = TelegramAlerter()

        # State
        self.positions: list[ArbPosition] = []
        self.session_start = time.time()
        self.scan_count = 0
        self.arbs_executed = 0
        self.total_profit = 0.0
        self.last_status_time = 0.0

    def _init_polymarket(self):
        """Initialize Polymarket client (lazy — avoids RPC call at import)."""
        if self.poly is None:
            self.poly = Polymarket()

    def _get_balance(self) -> float:
        """Get current USDC balance."""
        if self.simulated_balance is not None:
            # Adjust for simulated trades
            spent = sum(p.total_cost for p in self.positions if p.status == "open" and p.is_dry_run)
            profit = sum(p.actual_profit for p in self.positions if p.actual_profit is not None and p.is_dry_run)
            return self.simulated_balance - spent + profit
        self._init_polymarket()
        return self.poly.get_usdc_balance()

    def scan_opportunities(self) -> list[ArbOpportunity]:
        """
        Two-pass scan for arbitrage opportunities.

        Pass 1: API price screen (cheap — uses gamma API prices)
        Pass 2: Order book verification (only for candidates)
        """
        self._init_polymarket()

        # Pass 1: Get all negRisk events and screen by API prices
        events = self.gamma.get_negrisk_events()
        logger.info(f"Pass 1: Scanned {len(events)} negRisk events")

        now = datetime.now(timezone.utc)
        candidates = []
        for event in events:
            total = event["_total_yes_price"]
            num = event["_num_outcomes"]

            # Quick screen: total YES price must be below threshold
            if total >= (1.0 - self.config.min_spread):
                continue

            # Filter by resolution date
            end_date_str = event.get("endDate", "")
            if end_date_str:
                try:
                    end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                    days_remaining = (end_dt - now).total_seconds() / 86400
                    if days_remaining > self.config.max_days_to_resolution:
                        continue
                    if days_remaining < 0:
                        continue
                except Exception:
                    pass

            # Skip if any outcome is priced too high (likely already resolved)
            outcomes = event["_outcomes"]
            if any(o["yes_price"] > self.config.max_outcome_price for o in outcomes):
                continue

            # Skip if not all markets are accepting orders
            if not all(o["accepting_orders"] for o in outcomes):
                continue

            # Skip closed markets
            if any(o.get("closed", False) for o in outcomes):
                continue

            if num < self.config.min_outcomes:
                continue

            spread = 1.0 - total
            logger.info(f"  Candidate: {event['title'][:60]}... ({num} outcomes, API spread={spread:.4f})")
            candidates.append(event)

        if not candidates:
            return []

        # Pass 2: Verify with real order book prices
        logger.info(f"Pass 2: Verifying {len(candidates)} candidates with order books")
        opportunities = []

        for event in candidates:
            outcomes = event["_outcomes"]
            verified_outcomes = []
            total_best_ask = 0.0
            min_depth = float("inf")

            for outcome in outcomes:
                token_id = outcome["yes_token_id"]
                if not token_id:
                    break

                best_ask = self.poly.get_best_ask(token_id)
                if best_ask is None:
                    logger.debug(f"    No ask for {outcome['label'][:30]}")
                    break

                # Get book depth at best ask
                try:
                    book = self.poly.client.get_order_book(token_id)
                    depth_at_best = sum(
                        float(a.size) for a in book.asks
                        if float(a.price) <= best_ask + self.config.price_buffer
                    )
                except Exception:
                    depth_at_best = 0

                if depth_at_best < self.config.min_leg_size:
                    logger.debug(f"    Insufficient depth for {outcome['label'][:30]}: {depth_at_best:.1f}")
                    break

                min_depth = min(min_depth, depth_at_best)
                total_best_ask += best_ask
                verified_outcomes.append({
                    **outcome,
                    "best_ask": best_ask,
                    "depth": depth_at_best,
                })

            # Must have verified all outcomes
            if len(verified_outcomes) != len(outcomes):
                continue

            # Calculate effective cost per share including buffer + rounding
            total_effective = sum(
                round(o["best_ask"] + self.config.price_buffer, 2)
                for o in verified_outcomes
            )
            spread = 1.0 - total_effective
            if spread < self.config.min_spread:
                logger.info(f"    {event['title'][:50]}... effective spread={spread:.4f} < min_spread, skipping")
                continue

            # Calculate position size using effective cost
            balance = self._get_balance()
            max_from_balance = balance * self.config.max_position_pct
            max_total = min(self.config.max_position_size, max_from_balance)
            max_shares = max_total / total_effective
            shares = min(max_shares, min_depth)

            if shares < self.config.min_leg_size:
                logger.info(f"    {event['title'][:50]}... shares={shares:.1f} < min_leg_size, skipping")
                continue

            opp = ArbOpportunity(
                event_id=event["id"],
                event_title=event["title"],
                event_slug=event["slug"],
                num_outcomes=len(verified_outcomes),
                total_ask_price=total_effective,
                spread=spread,
                max_shares=min_depth,
                suggested_size=shares,
                outcomes=verified_outcomes,
            )
            opportunities.append(opp)
            logger.info(
                f"  ARB FOUND: {event['title'][:50]}... "
                f"spread=${spread:.4f} ({spread*100:.1f}%), "
                f"{len(verified_outcomes)} outcomes, "
                f"max {shares:.1f} shares"
            )

        return opportunities

    async def execute_arb(self, opp: ArbOpportunity) -> Optional[ArbPosition]:
        """
        Execute an arbitrage by buying equal shares of every outcome.

        Returns ArbPosition on success (even partial), None on failure.
        """
        self._init_polymarket()

        # Pre-flight: re-check balance
        balance = self._get_balance()
        total_cost_estimate = opp.suggested_size * opp.total_ask_price
        if total_cost_estimate > balance * 0.95:
            logger.warning(f"Insufficient balance: need ${total_cost_estimate:.2f}, have ${balance:.2f}")
            return None

        shares = opp.suggested_size
        position = ArbPosition(
            event_id=opp.event_id,
            event_title=opp.event_title,
            event_slug=opp.event_slug,
            timestamp=datetime.now(timezone.utc).isoformat(),
            shares=shares,
            is_dry_run=self.dry_run,
        )

        legs: list[ArbLeg] = []
        total_cost = 0.0

        for outcome in opp.outcomes:
            price = outcome["best_ask"] + self.config.price_buffer
            # Round price to 2 decimal places (Polymarket tick size)
            price = round(price, 2)
            cost = shares * price

            leg = ArbLeg(
                label=outcome["label"],
                token_id=outcome["yes_token_id"],
                condition_id=outcome["condition_id"],
                market_id=outcome["market_id"],
                price=price,
                shares=shares,
                cost=cost,
            )

            if self.dry_run:
                leg.filled = True
                leg.fill_price = price
                leg.order_id = f"DRY_{outcome['market_id']}_{int(time.time())}"
                logger.info(f"  [DRY] Buy {shares:.1f} x {outcome['label'][:30]} @ ${price:.3f} = ${cost:.2f}")
            else:
                try:
                    resp = self.poly.execute_limit_buy(
                        token_id=outcome["yes_token_id"],
                        price=price,
                        size=shares,
                    )
                    order_id = resp.get("orderID", "") if isinstance(resp, dict) else str(resp)
                    leg.order_id = order_id
                    logger.info(f"  Order placed: {outcome['label'][:30]} @ ${price:.3f}, order_id={order_id}")
                except Exception as e:
                    logger.error(f"  Failed to place order for {outcome['label'][:30]}: {e}")
                    leg.order_id = None

            legs.append(leg)
            total_cost += cost

        # Monitor fills for live orders
        if not self.dry_run:
            await self._monitor_fills(legs)

        position.legs = legs
        position.total_cost = total_cost
        position.expected_profit = shares * 1.0 - total_cost

        filled_count = sum(1 for leg in legs if leg.filled)
        if filled_count == len(legs):
            position.status = "open"
        elif filled_count > 0:
            position.status = "partial"
            if self.config.cancel_on_partial:
                await self._cancel_unfilled(legs)
        else:
            logger.warning("No legs filled — arb not executed")
            return None

        self.positions.append(position)
        self.arbs_executed += 1

        # Log and alert
        mode = "DRY" if self.dry_run else "LIVE"
        msg = (
            f"{'[DRY] ' if self.dry_run else ''}ARB EXECUTED\n"
            f"Event: {opp.event_title}\n"
            f"Outcomes: {opp.num_outcomes}\n"
            f"Shares: {shares:.1f}\n"
            f"Total cost: ${total_cost:.2f}\n"
            f"Expected profit: ${position.expected_profit:.2f} ({opp.spread*100:.1f}%)\n"
            f"Filled: {filled_count}/{len(legs)}"
        )
        logger.info(msg)

        alert_msg = (
            f"{'🧪 ' if self.dry_run else '💰 '}<b>NegRisk Arb {mode}</b>\n\n"
            f"<b>{opp.event_title}</b>\n"
            f"Outcomes: {opp.num_outcomes} | Shares: {shares:.1f}\n"
            f"Cost: ${total_cost:.2f} | Profit: ${position.expected_profit:.2f} ({opp.spread*100:.1f}%)\n"
            f"Filled: {filled_count}/{len(legs)}"
        )
        await self.alerter.send_message(alert_msg)

        return position

    async def _monitor_fills(self, legs: list[ArbLeg]):
        """Poll order status for fill detection."""
        self._init_polymarket()
        deadline = time.time() + self.config.order_timeout

        while time.time() < deadline:
            all_filled = True
            for leg in legs:
                if leg.filled or not leg.order_id:
                    continue
                try:
                    order = self.poly.get_order(leg.order_id)
                    status = order.get("status", "") if isinstance(order, dict) else ""
                    if status in ("MATCHED", "FILLED"):
                        leg.filled = True
                        leg.fill_price = float(order.get("price", leg.price)) if isinstance(order, dict) else leg.price
                        logger.info(f"    Filled: {leg.label[:30]} @ ${leg.fill_price:.3f}")
                    else:
                        all_filled = False
                except Exception:
                    all_filled = False

            if all_filled:
                break
            await asyncio.sleep(1.0)

    async def _cancel_unfilled(self, legs: list[ArbLeg]):
        """Cancel unfilled legs."""
        self._init_polymarket()
        for leg in legs:
            if not leg.filled and leg.order_id:
                try:
                    self.poly.cancel_order(leg.order_id)
                    logger.info(f"    Cancelled unfilled: {leg.label[:30]}")
                except Exception as e:
                    logger.error(f"    Failed to cancel {leg.label[:30]}: {e}")

    def check_resolutions(self):
        """Check if any open positions have resolved."""
        for pos in self.positions:
            if pos.status not in ("open", "partial"):
                continue

            # Check via gamma API if the event is resolved
            try:
                events = self.gamma.get_events(
                    querystring_params={"id": pos.event_id}
                )
                if not events:
                    continue
                event = events[0]
            except Exception:
                continue

            markets = event.get("markets", [])
            if not markets:
                continue

            # Check if all markets are closed
            all_closed = all(m.get("closed", False) for m in markets)
            if not all_closed:
                continue

            # Find the winning outcome
            winning_leg = None
            for leg in pos.legs:
                for mkt in markets:
                    if str(mkt.get("id", "")) == leg.market_id:
                        prices = mkt.get("outcomePrices", "[]")
                        if isinstance(prices, str):
                            import json
                            try:
                                prices = json.loads(prices)
                            except Exception:
                                continue
                        if prices and float(prices[0]) >= 0.95:
                            winning_leg = leg
                            break

            if winning_leg is None:
                continue

            # Calculate P&L
            payout = pos.shares * 1.0
            pos.actual_profit = payout - pos.total_cost
            pos.status = "won" if pos.actual_profit > 0 else "lost"
            self.total_profit += pos.actual_profit

            logger.info(
                f"RESOLVED: {pos.event_title[:50]}... "
                f"Winner: {winning_leg.label[:30]} | "
                f"Payout: ${payout:.2f} - Cost: ${pos.total_cost:.2f} = "
                f"{'+'if pos.actual_profit >= 0 else ''}${pos.actual_profit:.2f}"
            )

            # Alert
            asyncio.get_event_loop().create_task(
                self.alerter.send_message(
                    f"{'✅' if pos.actual_profit > 0 else '❌'} <b>NegRisk Arb Resolved</b>\n\n"
                    f"<b>{pos.event_title}</b>\n"
                    f"Winner: {winning_leg.label}\n"
                    f"P&L: {'+'if pos.actual_profit >= 0 else ''}${pos.actual_profit:.2f}\n"
                    f"Session total: {'+'if self.total_profit >= 0 else ''}${self.total_profit:.2f}"
                )
            )

    async def _log_status(self):
        """Periodic status update."""
        now = time.time()
        if now - self.last_status_time < self.config.status_interval:
            return
        self.last_status_time = now

        elapsed = now - self.session_start
        hours = elapsed / 3600
        balance = self._get_balance()

        open_positions = [p for p in self.positions if p.status in ("open", "partial")]
        resolved = [p for p in self.positions if p.status in ("won", "lost")]

        status = (
            f"--- NegRisk Arb Status ---\n"
            f"Runtime: {hours:.1f}h | Scans: {self.scan_count}\n"
            f"Balance: ${balance:.2f} | Mode: {'DRY' if self.dry_run else 'LIVE'}\n"
            f"Arbs executed: {self.arbs_executed} | Open: {len(open_positions)} | Resolved: {len(resolved)}\n"
            f"Session P&L: {'+'if self.total_profit >= 0 else ''}${self.total_profit:.2f}"
        )
        logger.info(status)

        if self.scan_count > 0 and self.scan_count % 10 == 0:
            await self.alerter.send_message(
                f"📊 <b>NegRisk Arb Status</b>\n\n"
                f"Runtime: {hours:.1f}h | Scans: {self.scan_count}\n"
                f"Balance: ${balance:.2f}\n"
                f"Arbs: {self.arbs_executed} | Open: {len(open_positions)}\n"
                f"P&L: {'+'if self.total_profit >= 0 else ''}${self.total_profit:.2f}"
            )

    async def run(
        self,
        scan_interval: Optional[float] = None,
        max_iterations: Optional[int] = None,
    ):
        """Main run loop."""
        interval = scan_interval or self.config.scan_interval
        balance = self._get_balance()

        # Startup banner
        mode = "DRY RUN" if self.dry_run else "LIVE"
        banner = (
            f"\n{'='*60}\n"
            f"  NegRisk Multi-Outcome Arbitrage Bot [{mode}]\n"
            f"{'='*60}\n"
            f"  Balance: ${balance:.2f}\n"
            f"  Min spread: {self.config.min_spread*100:.1f}%\n"
            f"  Max position: ${self.config.max_position_size:.0f}\n"
            f"  Scan interval: {interval}s\n"
            f"  Min outcomes: {self.config.min_outcomes}\n"
            f"  Max days to resolution: {self.config.max_days_to_resolution:.0f}\n"
            f"{'='*60}\n"
        )
        logger.info(banner)

        await self.alerter.send_message(
            f"🚀 <b>NegRisk Arb Bot Started [{mode}]</b>\n\n"
            f"Balance: ${balance:.2f}\n"
            f"Min spread: {self.config.min_spread*100:.1f}%\n"
            f"Max position: ${self.config.max_position_size:.0f}\n"
            f"Max resolution: {self.config.max_days_to_resolution:.0f} days\n"
            f"Scan interval: {interval}s"
        )

        iteration = 0
        try:
            while True:
                if max_iterations is not None and iteration >= max_iterations:
                    logger.info(f"Reached max iterations ({max_iterations}), stopping")
                    break

                iteration += 1
                self.scan_count += 1

                try:
                    # 1. Check resolutions
                    self.check_resolutions()

                    # 2. Scan for opportunities
                    opportunities = self.scan_opportunities()

                    # 3. Execute best opportunity (highest spread)
                    if opportunities:
                        best = max(opportunities, key=lambda o: o.spread)
                        logger.info(
                            f"Best arb: {best.event_title[:50]}... "
                            f"spread={best.spread*100:.1f}%"
                        )
                        await self.execute_arb(best)
                    else:
                        logger.info(f"Scan {self.scan_count}: No arb opportunities found")

                    # 4. Status update
                    await self._log_status()

                except Exception as e:
                    logger.error(f"Error in scan loop: {e}", exc_info=True)

                # 5. Sleep
                if max_iterations is None or iteration < max_iterations:
                    await asyncio.sleep(interval)

        except KeyboardInterrupt:
            logger.info("Shutting down...")
        finally:
            # Final summary
            summary = (
                f"\n{'='*60}\n"
                f"  NegRisk Arb Bot — Session Summary\n"
                f"{'='*60}\n"
                f"  Scans: {self.scan_count}\n"
                f"  Arbs executed: {self.arbs_executed}\n"
                f"  Total P&L: {'+'if self.total_profit >= 0 else ''}${self.total_profit:.2f}\n"
                f"  Open positions: {sum(1 for p in self.positions if p.status in ('open','partial'))}\n"
                f"{'='*60}\n"
            )
            logger.info(summary)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NegRisk Multi-Outcome Arbitrage Bot")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Simulate trades (default: True)")
    parser.add_argument("--live", action="store_true", help="Execute real trades")
    parser.add_argument("--simulated-balance", type=float, default=None, help="Use simulated balance")
    parser.add_argument("--scan-interval", type=float, default=30.0, help="Seconds between scans")
    parser.add_argument("--max-iterations", type=int, default=None, help="Max scan iterations")
    parser.add_argument("--min-spread", type=float, default=0.03, help="Min arb spread (default: 0.03 = 3%%)")
    parser.add_argument("--max-position", type=float, default=20.0, help="Max position size in USDC")
    parser.add_argument("--max-days", type=float, default=14.0, help="Max days to resolution (default: 14)")
    args = parser.parse_args()

    dry_run = not args.live
    config = NegRiskArbConfig(
        min_spread=args.min_spread,
        max_position_size=args.max_position,
        max_days_to_resolution=args.max_days,
    )
    bot = NegRiskArbBot(
        config=config,
        dry_run=dry_run,
        simulated_balance=args.simulated_balance,
    )
    asyncio.run(bot.run(
        scan_interval=args.scan_interval,
        max_iterations=args.max_iterations,
    ))
