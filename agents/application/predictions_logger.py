"""Predictions Logger — track directional v2 predictions and outcomes."""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(".env"))

logger = logging.getLogger(__name__)

LOG_PATH = Path(__file__).parent.parent / "data" / "predictions.json"


class PredictionsLogger:

    def __init__(self):
        self.log_path = LOG_PATH
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.predictions: list[dict] = []
        if self.log_path.exists():
            try:
                text = self.log_path.read_text().strip()
                if text:
                    self.predictions = json.loads(text)
            except (json.JSONDecodeError, OSError):
                self.predictions = []

    def log_prediction(self, result) -> None:
        """Log a new prediction. Skips if question already exists."""
        question = result.question if hasattr(result, "question") else result.get("question", "")
        if question in self.get_all_questions():
            logger.info(f"Skipping duplicate: {question[:50]}")
            return

        entry = {
            "market_id": result.market_id if hasattr(result, "market_id") else result.get("market_id", ""),
            "question": question,
            "recommendation": result.recommendation if hasattr(result, "recommendation") else result.get("recommendation", ""),
            "size_usdc": 0.0,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "pending",
            "pnl": None,
        }
        self.predictions.append(entry)
        self._save()
        logger.info(f"Logged prediction: {question[:50]}")

    def get_pending(self) -> list:
        """Return predictions with status 'pending'."""
        return [p for p in self.predictions if p.get("status") == "pending"]

    def get_all_questions(self) -> set:
        """Return all questions ever logged, regardless of status."""
        return {p.get("question", "") for p in self.predictions}

    def sync_with_polymarket(self, wallet_address: str) -> list:
        """Check pending predictions for fills and resolution.

        Queries Polymarket Data API to verify actual trades and outcomes.

        Returns:
            List of newly resolved prediction dicts.
        """
        pending = self.get_pending()
        if not pending:
            return []

        # Fetch recent activity once (avoid per-market queries)
        try:
            resp = httpx.get(
                f"https://data-api.polymarket.com/activity",
                params={"user": wallet_address, "limit": 500},
                timeout=15,
            )
            if resp.status_code != 200:
                logger.warning(f"Activity API returned {resp.status_code}")
                return []
            data = resp.json()
            if isinstance(data, dict):
                activity = data.get("data") or data.get("activities") or data.get("results") or []
            elif isinstance(data, list):
                activity = data
            else:
                activity = []
        except Exception as e:
            logger.warning(f"Failed to fetch activity: {e}")
            return []

        # Index BUY fills by conditionId
        fills_by_market: dict[str, list] = {}
        for item in activity:
            if (item.get("side") or "").upper() != "BUY":
                continue
            cid = item.get("conditionId") or ""
            if cid:
                fills_by_market.setdefault(cid, []).append(item)

        newly_resolved = []
        for pred in pending:
            market_id = pred.get("market_id", "")
            if not market_id:
                continue

            try:
                # Check if market is closed via Gamma API
                resp = httpx.get(
                    "https://gamma-api.polymarket.com/markets",
                    params={"conditionId": market_id},
                    timeout=10,
                )
                if resp.status_code != 200:
                    continue
                markets = resp.json()
                if not markets:
                    continue
                market = markets[0] if isinstance(markets, list) else markets
                if not market.get("closed", False):
                    continue

                # Market is closed — check if we have a fill
                market_fills = fills_by_market.get(market_id, [])
                if not market_fills:
                    pred["status"] = "unfilled"
                    self._save()
                    logger.warning(f"Order was not filled: {pred.get('question', '')[:50]}")
                    newly_resolved.append(pred)
                    continue

                # Calculate fill size and avg price
                total_size = 0.0
                total_cost = 0.0
                for f in market_fills:
                    size = float(f.get("size") or 0)
                    price = float(f.get("price") or 0)
                    if size > 0:
                        total_size += size
                        total_cost += size * price
                avg_price = total_cost / total_size if total_size > 0 else 0

                # Determine outcome from final prices
                outcome_prices = market.get("outcomePrices", "[]")
                if isinstance(outcome_prices, str):
                    outcome_prices = json.loads(outcome_prices)
                if outcome_prices and len(outcome_prices) >= 2:
                    yes_price = float(outcome_prices[0])
                    outcome = "YES" if yes_price > 0.5 else "NO"
                else:
                    outcome = "UNKNOWN"

                # Did we win?
                rec = pred.get("recommendation", "")
                won = (rec == "BUY_YES" and outcome == "YES") or (rec == "BUY_NO" and outcome == "NO")

                if won:
                    pnl = round(total_size * (1.0 - avg_price), 2)
                    pred["status"] = "won"
                else:
                    pnl = round(-total_size * avg_price, 2)
                    pred["status"] = "lost"

                pred["pnl"] = pnl
                pred["size_usdc"] = round(total_cost, 2)
                self._save()
                newly_resolved.append(pred)
                logger.info(
                    f"Resolved: {pred.get('question', '')[:50]} | "
                    f"{pred['status']} | filled={total_size:.2f}@${avg_price:.2f} | pnl=${pnl:.2f}"
                )

            except Exception as e:
                logger.debug(f"Error checking {market_id[:16]}: {e}")
                continue

        return newly_resolved

    def get_stats(self) -> dict:
        """Stats based only on verified fills (won/lost). Excludes unfilled."""
        won = [p for p in self.predictions if p.get("status") == "won"]
        lost = [p for p in self.predictions if p.get("status") == "lost"]
        unfilled = [p for p in self.predictions if p.get("status") == "unfilled"]
        pending = [p for p in self.predictions if p.get("status") == "pending"]

        resolved = won + lost
        win_rate = len(won) / len(resolved) if resolved else 0.0
        total_pnl = sum(p.get("pnl") or 0 for p in resolved)

        return {
            "total": len(self.predictions),
            "won": len(won),
            "lost": len(lost),
            "unfilled": len(unfilled),
            "pending": len(pending),
            "win_rate": win_rate,
            "total_pnl": total_pnl,
        }

    def _save(self) -> None:
        self.log_path.write_text(json.dumps(self.predictions, indent=2) + "\n")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    pl = PredictionsLogger()
    print(f"Predictions: {len(pl.predictions)}")
    print(f"Pending: {len(pl.get_pending())}")
    print(f"All questions: {len(pl.get_all_questions())}")
    print(f"Stats: {pl.get_stats()}")
