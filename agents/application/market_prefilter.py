"""Market pre-filter using free Gamma API — no Claude calls, no cost.

Narrows Polymarket markets to promising candidates before expensive
Claude evaluation.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(".env"))

from agents.polymarket.gamma import GammaMarketClient

logger = logging.getLogger(__name__)


class MarketPrefilter:
    """Pre-filters markets using free Gamma API data."""

    def __init__(self):
        self.gamma = GammaMarketClient()
        self.min_volume: float = 5000
        self.min_price: float = 0.08
        self.max_price: float = 0.92
        self.max_days_to_resolution: int = 30
        self.min_liquidity: float = 500

    def get_candidates(self, max_candidates: int = 15) -> list[dict]:
        """Fetch and filter markets to best candidates for Claude evaluation.

        Args:
            max_candidates: Maximum number of candidates to return

        Returns:
            List of market dicts sorted by volume descending
        """
        markets = self.gamma.get_tradeable_markets(limit=200)
        total = len(markets)
        now = datetime.now(timezone.utc)
        candidates = []

        for market in markets:
            market_dict = market.model_dump() if hasattr(market, "model_dump") else dict(market)

            # 1. Resolution date within max_days_to_resolution
            end_date_str = (
                market_dict.get("endDate")
                or market_dict.get("end_date")
                or market_dict.get("endDateIso")
                or market_dict.get("gameStartTime")
            )
            if end_date_str:
                try:
                    end_date = datetime.fromisoformat(str(end_date_str).replace("Z", "+00:00"))
                    days_to_resolution = (end_date - now).days
                    if days_to_resolution > self.max_days_to_resolution:
                        continue
                except (ValueError, TypeError):
                    pass

            # 2. Price between min_price and max_price
            outcome_prices = market_dict.get("outcomePrices")
            if outcome_prices:
                if isinstance(outcome_prices, str):
                    try:
                        outcome_prices = json.loads(outcome_prices)
                    except (json.JSONDecodeError, TypeError):
                        outcome_prices = []
                if outcome_prices:
                    try:
                        yes_price = float(outcome_prices[0])
                    except (IndexError, ValueError, TypeError):
                        yes_price = 0.5
                else:
                    yes_price = 0.5
            else:
                yes_price = 0.5

            if yes_price < self.min_price or yes_price > self.max_price:
                continue

            # 3. Volume above min_volume
            volume = market_dict.get("volume24hr") or market_dict.get("volumeClob") or 0
            try:
                volume = float(volume)
            except (ValueError, TypeError):
                volume = 0
            if volume < self.min_volume:
                continue

            market_dict["_volume"] = volume
            candidates.append(market_dict)

        # Sort by volume descending
        candidates.sort(key=lambda m: m.get("_volume", 0), reverse=True)
        candidates = candidates[:max_candidates]

        logger.info(f"Pre-filter: {total} markets → {len(candidates)} candidates after filtering")
        return candidates


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    prefilter = MarketPrefilter()
    candidates = prefilter.get_candidates(max_candidates=15)
    print(f"Found {len(candidates)} candidates:")
    for m in candidates:
        print(f"  {m.get('question', m.get('title', ''))[:70]}")
