"""
Claude Forecaster — scan Polymarket markets for mispriced opportunities.

Uses AnthropicClient (Claude with web search) to estimate true probabilities
and compare against current market prices. Returns actionable signals.

Usage:
    python -m agents.application.claude_forecaster
"""

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(".env"))

from agents.connectors.anthropic_client import AnthropicClient
from agents.application.market_prefilter import MarketPrefilter
from agents.polymarket.gamma import GammaMarketClient
from agents.polymarket.polymarket import Polymarket

logger = logging.getLogger(__name__)


@dataclass
class ForecasterResult:
    market_id: str
    question: str
    current_price: float
    claude_probability: float
    edge: float  # positive = YES underpriced, negative = NO underpriced
    confidence: str
    reasoning: str
    recommendation: str  # "BUY_YES", "BUY_NO", or "SKIP"
    timestamp: str = ""  # ISO format
    confidence_score: int = 3  # 1-5 scale from Claude Stage 2
    clob_token_ids: list = field(default_factory=list)  # [YES_token, NO_token]
    days_to_resolution: int = 0


class ClaudeForecaster:
    """Scans Polymarket markets and uses Claude to find mispriced ones."""

    CONFIDENCE_SIZE_MAP = {1: 0, 2: 0, 3: 15, 4: 25, 5: 40}

    @staticmethod
    def size_for_confidence(confidence_score: int) -> float:
        """Map confidence score (1-5) to trade size in USDC. 1-2 = skip."""
        return ClaudeForecaster.CONFIDENCE_SIZE_MAP.get(confidence_score, 15)

    def __init__(self, min_edge: float = 0.10, min_confidence: str = "medium", api_delay: int = 60, max_days_to_resolution: int = 30):
        self.min_edge = min_edge
        self.min_confidence = min_confidence
        self.anthropic = AnthropicClient()
        self.gamma = GammaMarketClient()
        self.api_delay = api_delay  # seconds between API calls
        self.max_days_to_resolution = max_days_to_resolution

    def evaluate_single_market(self, market: dict) -> ForecasterResult | None:
        question = market.get("question") or market.get("title") or ""
        if not question:
            return None

        # Extract current YES price
        outcome_prices = market.get("outcomePrices")
        if outcome_prices:
            try:
                if isinstance(outcome_prices, str):
                    import json
                    prices = json.loads(outcome_prices)
                else:
                    prices = outcome_prices
                current_price = float(prices[0])
            except (IndexError, ValueError, TypeError):
                current_price = 0.5
        else:
            current_price = 0.5

        # Skip markets already near resolution
        if current_price < 0.02 or current_price > 0.98:
            return None

        # Resolution date filter
        days_to_resolution = 0
        end_date_str = (
            market.get("endDate")
            or market.get("end_date")
            or market.get("endDateIso")
            or market.get("gameStartTime")
        )
        if end_date_str:
            try:
                end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                days_to_resolution = max(0, (end_date - datetime.now(timezone.utc)).days)
                if days_to_resolution < 2:
                    logger.debug(f"Skipping {question[:50]} - resolves in {days_to_resolution} days (too soon)")
                    return None
                if days_to_resolution > self.max_days_to_resolution:
                    logger.debug(f"Skipping {question[:50]} - resolves in {days_to_resolution} days")
                    return None
            except (ValueError, TypeError):
                pass

        market_id = market.get("condition_id") or market.get("conditionId") or ""

        # Extract CLOB token IDs for order execution
        clob_token_ids = market.get("clobTokenIds") or []
        if isinstance(clob_token_ids, str):
            try:
                clob_token_ids = json.loads(clob_token_ids)
            except (json.JSONDecodeError, TypeError):
                clob_token_ids = []
        if not isinstance(clob_token_ids, list):
            clob_token_ids = []

        # Stage 1: Cheap quick estimate (no web search)
        quick = self.anthropic.quick_estimate(question, current_price)
        if quick is None:
            logger.info(f"Stage 1 quick estimate: {question[:40]} | failed - SKIP")
            return None

        quick_edge = abs(quick["edge"])
        passes = quick_edge >= self.min_edge
        logger.info(f"Stage 1 quick estimate: {question[:40]} | edge={quick['edge']:.2f} - {'PASS' if passes else 'SKIP'}")
        if not passes:
            return None

        # Stage 2: Full web search evaluation (expensive)
        result = self.anthropic.evaluate_market(question, current_price)
        if result is None:
            return None

        edge = result["edge"]
        confidence = result["confidence"]
        confidence_score = result.get("confidence_score", 3)
        reasoning = result["reasoning"]
        claude_probability = result["probability"]

        if edge >= self.min_edge:
            recommendation = "BUY_YES"
        elif edge <= -self.min_edge:
            recommendation = "BUY_NO"
        else:
            recommendation = "SKIP"

        forecaster_result = ForecasterResult(
            market_id=market_id,
            question=question,
            current_price=current_price,
            claude_probability=claude_probability,
            edge=edge,
            confidence=confidence,
            reasoning=reasoning,
            recommendation=recommendation,
            confidence_score=confidence_score,
            timestamp=datetime.now(timezone.utc).isoformat(),
            clob_token_ids=clob_token_ids,
            days_to_resolution=days_to_resolution,
        )

        logger.info(
            f"{question[:60]} | price={current_price:.2f} | "
            f"claude={claude_probability:.2f} | edge={edge:+.2f} | {recommendation}"
        )

        return forecaster_result

    def execute_opportunity(
        self, result: ForecasterResult, size_usdc: float, dry_run: bool = True
    ) -> dict:
        """Execute a trade based on a forecaster result.

        Args:
            result: The ForecasterResult with recommendation
            size_usdc: USDC amount to trade
            dry_run: If True, only log what would happen

        Returns:
            Dict with status and details
        """
        if result.recommendation == "SKIP":
            return {"status": "skipped"}

        if dry_run:
            logger.info(
                f"DRY RUN: Would place {result.recommendation} order on "
                f"{result.question[:50]} for ${size_usdc:.2f}"
            )
            return {
                "status": "dry_run",
                "recommendation": result.recommendation,
                "size": size_usdc,
            }

        if len(result.clob_token_ids) != 2:
            logger.error(f"Missing CLOB token IDs for {result.question[:50]}")
            return {"status": "error", "error": "Missing CLOB token IDs"}

        try:
            if result.recommendation == "BUY_NO":
                token_id = result.clob_token_ids[1]  # NO token
                price = round(1.0 - result.current_price - 0.01, 2)
            elif result.recommendation == "BUY_YES":
                token_id = result.clob_token_ids[0]  # YES token
                price = round(result.current_price + 0.01, 2)
            else:
                return {"status": "skipped"}

            # Clamp price to valid range
            price = max(0.01, min(0.99, price))
            size = size_usdc / price  # Convert USDC to shares

            poly = Polymarket()
            order_result = poly.execute_limit_buy(
                token_id=token_id, price=price, size=size
            )

            logger.info(
                f"ORDER PLACED: {result.recommendation} on {result.question[:50]} | "
                f"${size_usdc:.2f} at ${price:.2f} | order={order_result}"
            )
            return {"status": "executed", "order": order_result}

        except Exception as e:
            logger.error(f"Order execution failed: {e}")
            return {"status": "error", "error": str(e)}

    def scan_markets(self, limit: int = 20) -> list[ForecasterResult]:
        prefilter = MarketPrefilter()
        markets = prefilter.get_candidates(max_candidates=limit)
        results: list[ForecasterResult] = []

        total = len(markets)
        for i, market_dict in enumerate(markets):
            question = market_dict.get("question") or market_dict.get("title") or ""
            result = self.evaluate_single_market(market_dict)
            if result is not None:
                results.append(result)
            logger.info(f"Evaluated market {i+1}/{total}: {question[:50]}")
            if i < total - 1:
                time.sleep(self.api_delay)

        results.sort(key=lambda r: abs(r.edge), reverse=True)
        logger.info(f"Scanned {len(markets[:limit])} markets, found {len(results)} opportunities")
        return results

    def get_opportunities(self, limit: int = 20) -> list[ForecasterResult]:
        results = self.scan_markets(limit)
        return [r for r in results if r.recommendation != "SKIP"]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    forecaster = ClaudeForecaster(min_edge=0.10, min_confidence="medium")
    print("Scanning 5 markets for opportunities...")
    results = forecaster.scan_markets(limit=5)
    print(f"\nFound {len(results)} results:")
    for r in results:
        print(f"  {r.recommendation} | {r.question[:60]} | edge={r.edge:.2f}")
