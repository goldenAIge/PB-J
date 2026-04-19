import httpx
import json

from agents.polymarket.polymarket import Polymarket
from agents.utils.objects import Market, PolymarketEvent, ClobReward, Tag


class GammaMarketClient:
    def __init__(self):
        self.gamma_url = "https://gamma-api.polymarket.com"
        self.gamma_markets_endpoint = self.gamma_url + "/markets"
        self.gamma_events_endpoint = self.gamma_url + "/events"

    def parse_pydantic_market(self, market_object: dict) -> Market:
        try:
            if "clobRewards" in market_object:
                clob_rewards: list[ClobReward] = []
                for clob_rewards_obj in market_object["clobRewards"]:
                    clob_rewards.append(ClobReward(**clob_rewards_obj))
                market_object["clobRewards"] = clob_rewards

            if "events" in market_object:
                events: list[PolymarketEvent] = []
                for market_event_obj in market_object["events"]:
                    events.append(self.parse_nested_event(market_event_obj))
                market_object["events"] = events

            # These two fields below are returned as stringified lists from the api
            if "outcomePrices" in market_object:
                market_object["outcomePrices"] = json.loads(
                    market_object["outcomePrices"]
                )
            if "clobTokenIds" in market_object:
                market_object["clobTokenIds"] = json.loads(
                    market_object["clobTokenIds"]
                )

            return Market(**market_object)
        except Exception as err:
            print(f"[parse_market] Caught exception: {err}")
            print("exception while handling object:", market_object)

    # Event parser for events nested under a markets api response
    def parse_nested_event(self, event_object: dict()) -> PolymarketEvent:
        print("[parse_nested_event] called with:", event_object)
        try:
            if "tags" in event_object:
                print("tags here", event_object["tags"])
                tags: list[Tag] = []
                for tag in event_object["tags"]:
                    tags.append(Tag(**tag))
                event_object["tags"] = tags

            return PolymarketEvent(**event_object)
        except Exception as err:
            print(f"[parse_event] Caught exception: {err}")
            print("\n", event_object)

    def parse_pydantic_event(self, event_object: dict) -> PolymarketEvent:
        try:
            if "tags" in event_object:
                print("tags here", event_object["tags"])
                tags: list[Tag] = []
                for tag in event_object["tags"]:
                    tags.append(Tag(**tag))
                event_object["tags"] = tags
            return PolymarketEvent(**event_object)
        except Exception as err:
            print(f"[parse_event] Caught exception: {err}")

    def get_markets(
        self, querystring_params={}, parse_pydantic=False, local_file_path=None
    ) -> "list[Market]":
        if parse_pydantic and local_file_path is not None:
            raise Exception(
                'Cannot use "parse_pydantic" and "local_file" params simultaneously.'
            )

        response = httpx.get(self.gamma_markets_endpoint, params=querystring_params)
        if response.status_code == 200:
            data = response.json()
            if local_file_path is not None:
                with open(local_file_path, "w+") as out_file:
                    json.dump(data, out_file)
            elif not parse_pydantic:
                return data
            else:
                markets: list[Market] = []
                for market_object in data:
                    markets.append(self.parse_pydantic_market(market_object))
                return markets
        else:
            print(f"Error response returned from api: HTTP {response.status_code}")
            raise Exception()

    def get_events(
        self, querystring_params={}, parse_pydantic=False, local_file_path=None
    ) -> "list[PolymarketEvent]":
        if parse_pydantic and local_file_path is not None:
            raise Exception(
                'Cannot use "parse_pydantic" and "local_file" params simultaneously.'
            )

        response = httpx.get(self.gamma_events_endpoint, params=querystring_params)
        if response.status_code == 200:
            data = response.json()
            if local_file_path is not None:
                with open(local_file_path, "w+") as out_file:
                    json.dump(data, out_file)
            elif not parse_pydantic:
                return data
            else:
                events: list[PolymarketEvent] = []
                for market_event_obj in data:
                    events.append(self.parse_event(market_event_obj))
                return events
        else:
            raise Exception()

    def get_all_markets(self, limit=2) -> "list[Market]":
        return self.get_markets(querystring_params={"limit": limit})

    def get_all_events(self, limit=2) -> "list[PolymarketEvent]":
        return self.get_events(querystring_params={"limit": limit})

    def get_current_markets(self, limit=4) -> "list[Market]":
        return self.get_markets(
            querystring_params={
                "active": True,
                "closed": False,
                "archived": False,
                "limit": limit,
            }
        )

    def get_all_current_markets(self, limit=100) -> "list[Market]":
        offset = 0
        all_markets = []
        while True:
            params = {
                "active": True,
                "closed": False,
                "archived": False,
                "limit": limit,
                "offset": offset,
            }
            market_batch = self.get_markets(querystring_params=params)
            all_markets.extend(market_batch)

            if len(market_batch) < limit:
                break
            offset += limit

        return all_markets

    def get_current_events(self, limit=4) -> "list[PolymarketEvent]":
        return self.get_events(
            querystring_params={
                "active": True,
                "closed": False,
                "archived": False,
                "limit": limit,
            }
        )

    def get_clob_tradable_markets(self, limit=2) -> "list[Market]":
        return self.get_markets(
            querystring_params={
                "active": True,
                "closed": False,
                "archived": False,
                "limit": limit,
                "enableOrderBook": True,
            }
        )

    def get_tradeable_markets(self, limit: int = 100, order_by: str = "volume24hr") -> "list[Market]":
        """Fetch active, tradeable markets sorted by volume/liquidity."""
        return self.get_markets(querystring_params={
            "active": True,
            "closed": False,
            "archived": False,
            "enableOrderBook": True,
            "limit": limit,
            "order": order_by,
            "ascending": False,
        })

    def get_crypto_markets(self, asset: str = "btc", window_minutes: int = 15, num_windows: int = 8) -> list[dict]:
        """
        Fetch active crypto up/down markets by constructing slugs.

        These markets are 'restricted' on Polymarket and don't appear in normal
        market listings. Instead we construct the event slug from the timestamp
        pattern: {asset}-updown-{window_minutes}m-{unix_timestamp}

        Args:
            asset: Crypto asset ("btc", "eth", "sol")
            window_minutes: Candle duration in minutes (5 or 15)
            num_windows: Number of windows ahead to check

        Returns:
            List of market dicts that are open and accepting orders,
            each tagged with _window_minutes for downstream use.
        """
        import time as _time
        from datetime import datetime, timezone

        asset_lower = asset.lower()
        now = datetime.now(timezone.utc)
        current_ts = int(now.timestamp())

        window_seconds = window_minutes * 60
        # Round down to nearest window boundary
        window_start = (current_ts // window_seconds) * window_seconds

        filtered = []

        # Check current window and several future windows
        for offset in range(num_windows):
            slug_ts = window_start + (offset * window_seconds)
            slug = f"{asset_lower}-updown-{window_minutes}m-{slug_ts}"

            try:
                response = httpx.get(
                    self.gamma_events_endpoint,
                    params={"slug": slug},
                    timeout=10,
                )
                if response.status_code != 200:
                    continue

                events = response.json()
                if not events:
                    continue

                event = events[0]
                # Skip closed markets
                if event.get("closed", False):
                    continue

                for market in event.get("markets", []):
                    # Must be accepting orders
                    if not market.get("acceptingOrders", False):
                        continue
                    # Must have token IDs
                    clob_ids = market.get("clobTokenIds", "")
                    if isinstance(clob_ids, str):
                        try:
                            clob_ids = json.loads(clob_ids)
                        except Exception:
                            continue
                    if not clob_ids or len(clob_ids) < 2:
                        continue
                    # Must have valid endDate in the future
                    end_str = market.get("endDate", "")
                    if not end_str:
                        continue
                    try:
                        end_date = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                        if end_date <= now:
                            continue
                    except Exception:
                        continue

                    # Tag with metadata for downstream use
                    market["_window_minutes"] = window_minutes
                    market["_asset"] = asset_lower
                    filtered.append(market)

            except Exception:
                continue

        return filtered

    def get_15min_crypto_markets(self, asset: str = "btc", num_windows: int = 8) -> list[dict]:
        """Backward-compatible wrapper for get_crypto_markets with 15-min windows."""
        return self.get_crypto_markets(asset=asset, window_minutes=15, num_windows=num_windows)

    def get_weather_markets(self) -> list[dict]:
        """
        Fetch active daily temperature markets from Polymarket.

        Returns a list of event dicts, each representing one city+date with
        7 sub-markets (temperature brackets). Only returns events that:
        - Match "highest temperature in" pattern
        - Have open, order-accepting sub-markets
        - Have endDate in the future

        Each returned event dict is enriched with:
        - _city: parsed city name
        - _date: target date string "YYYY-MM-DD"
        - _unit: "F" or "C"
        - _brackets: list of parsed bracket dicts with keys:
            market_id, group_item_title, bracket_low, bracket_high,
            yes_token_id, yes_price, condition_id, threshold_index
        """
        import re
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        all_events = []

        # Paginate through weather events
        for page_offset in range(0, 200, 20):
            try:
                response = httpx.get(
                    self.gamma_events_endpoint,
                    params={
                        "tag_slug": "weather",
                        "closed": False,
                        "active": True,
                        "limit": 20,
                        "offset": page_offset,
                    },
                    timeout=15,
                )
                if response.status_code != 200:
                    break
                batch = response.json()
                if not batch:
                    break
                all_events.extend(batch)
            except Exception:
                break

        # Filter to daily temperature events
        temp_events = []
        for event in all_events:
            title = event.get("title", "")
            if "highest temperature" not in title.lower():
                continue

            # Parse city and date from title
            # Pattern: "Highest temperature in {City} on {Month} {Day}?"
            match = re.match(
                r"Highest temperature in (.+?) on (\w+ \d+)\??",
                title,
                re.IGNORECASE,
            )
            if not match:
                continue

            city_name = match.group(1).strip()
            date_str = match.group(2).strip()  # e.g. "February 13"

            # Parse endDate to get the year and validate it's in the future
            end_date_str = event.get("endDate", "")
            if not end_date_str:
                continue
            try:
                end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                if end_dt <= now:
                    continue
                target_date = end_dt.strftime("%Y-%m-%d")
            except Exception:
                continue

            # Parse sub-markets into brackets
            markets = event.get("markets", [])
            if not markets:
                continue

            brackets = []
            has_accepting = False
            for mkt in markets:
                if not mkt.get("acceptingOrders", False):
                    continue
                has_accepting = True

                group_title = mkt.get("groupItemTitle", "")
                threshold_idx = mkt.get("groupItemThreshold", "")

                # Parse bracket bounds from groupItemTitle
                bracket_low, bracket_high, unit = self._parse_temp_bracket(
                    group_title, threshold_idx
                )

                # Parse stringified JSON fields
                clob_ids = mkt.get("clobTokenIds", "[]")
                if isinstance(clob_ids, str):
                    clob_ids = json.loads(clob_ids)
                prices = mkt.get("outcomePrices", "[]")
                if isinstance(prices, str):
                    prices = json.loads(prices)

                if len(clob_ids) < 2 or len(prices) < 2:
                    continue

                brackets.append({
                    "market_id": str(mkt.get("id", "")),
                    "group_item_title": group_title,
                    "bracket_low": bracket_low,
                    "bracket_high": bracket_high,
                    "yes_token_id": clob_ids[0],
                    "yes_price": float(prices[0]),
                    "condition_id": mkt.get("conditionId", ""),
                    "threshold_index": int(threshold_idx) if threshold_idx else 0,
                })

            if not has_accepting or not brackets:
                continue

            # Sort brackets by threshold index
            brackets.sort(key=lambda b: b["threshold_index"])
            unit = brackets[0]["bracket_high"]  # detect from parsing
            # Re-detect unit from groupItemTitle
            first_title = markets[0].get("groupItemTitle", "")
            unit = "F" if "°F" in first_title else "C"

            event["_city"] = city_name
            event["_date"] = target_date
            event["_unit"] = unit
            event["_brackets"] = brackets
            temp_events.append(event)

        return temp_events

    @staticmethod
    def _parse_temp_bracket(
        group_title: str, threshold_idx: str
    ) -> tuple[float, float, str]:
        """
        Parse temperature bracket bounds from groupItemTitle.

        Patterns:
            "33°F or below"  → (-inf, 33.5)  (include up to 33)
            "34-35°F"        → (33.5, 35.5)
            "44°F or higher" → (43.5, inf)
            "5°C"            → (4.5, 5.5)    (single degree)
            "-7°C or below"  → (-inf, -6.5)
            "8°C or higher"  → (7.5, inf)

        Returns (bracket_low, bracket_high, unit).
        Bounds use .5 offsets so integer temps fall cleanly into one bracket.
        """
        import re

        unit = "F" if "°F" in group_title else "C"
        inf = float("inf")

        # "X or below" pattern
        if "or below" in group_title.lower():
            match = re.search(r"(-?\d+)", group_title)
            if match:
                val = float(match.group(1))
                # Handle negative: check if minus sign before number
                if "-" in group_title.split("°")[0] and val > 0:
                    val = -val
                return (-inf, val + 0.5, unit)

        # "X or higher" pattern
        if "or higher" in group_title.lower():
            match = re.search(r"(-?\d+)", group_title)
            if match:
                val = float(match.group(1))
                if "-" in group_title.split("°")[0] and val > 0:
                    val = -val
                return (val - 0.5, inf, unit)

        # "X-Y°F" range pattern (US cities, 2-degree ranges)
        range_match = re.match(r"(-?\d+)-(-?\d+)°([FC])", group_title)
        if range_match:
            low = float(range_match.group(1))
            high = float(range_match.group(2))
            return (low - 0.5, high + 0.5, unit)

        # Single degree "X°C" pattern (non-US cities)
        single_match = re.match(r"(-?\d+)°([FC])", group_title)
        if single_match:
            val = float(single_match.group(1))
            return (val - 0.5, val + 0.5, unit)

        # Fallback
        return (-inf, inf, unit)

    def get_negrisk_events(self, limit: int = 100, max_pages: int = 20) -> list[dict]:
        """Fetch active negRisk events with 3+ outcomes for arb scanning.

        Args:
            limit: Events per page (max 100)
            max_pages: Max pages to fetch (default 20 = 2000 events)
        """
        all_events = []
        offset = 0

        for _ in range(max_pages):
            try:
                response = httpx.get(
                    self.gamma_events_endpoint,
                    params={
                        "active": True,
                        "closed": False,
                        "limit": limit,
                        "offset": offset,
                    },
                    timeout=15,
                )
                if response.status_code != 200:
                    break
                batch = response.json()
                if not batch:
                    break
                all_events.extend(batch)
                if len(batch) < limit:
                    break
                offset += limit
            except Exception:
                break

        result = []
        for event in all_events:
            markets = event.get("markets", [])
            if len(markets) < 3:
                continue

            # Check if any market in this event is negRisk
            has_neg_risk = any(m.get("negRisk", False) for m in markets)
            if not has_neg_risk:
                continue

            outcomes = []
            for mkt in markets:
                # Parse stringified JSON fields
                clob_ids = mkt.get("clobTokenIds", "[]")
                if isinstance(clob_ids, str):
                    try:
                        clob_ids = json.loads(clob_ids)
                    except Exception:
                        continue
                prices = mkt.get("outcomePrices", "[]")
                if isinstance(prices, str):
                    try:
                        prices = json.loads(prices)
                    except Exception:
                        continue

                if not clob_ids or not prices:
                    continue

                yes_price = float(prices[0]) if prices else 0.0
                yes_token_id = clob_ids[0] if clob_ids else ""

                outcomes.append({
                    "label": mkt.get("groupItemTitle", mkt.get("question", "")),
                    "yes_price": yes_price,
                    "yes_token_id": yes_token_id,
                    "condition_id": mkt.get("conditionId", ""),
                    "market_id": str(mkt.get("id", "")),
                    "accepting_orders": mkt.get("acceptingOrders", False),
                    "closed": mkt.get("closed", False),
                })

            if len(outcomes) < 3:
                continue

            total_yes = sum(o["yes_price"] for o in outcomes)
            result.append({
                "id": str(event.get("id", "")),
                "title": event.get("title", ""),
                "slug": event.get("slug", ""),
                "endDate": event.get("endDate", ""),
                "_outcomes": outcomes,
                "_total_yes_price": total_yes,
                "_num_outcomes": len(outcomes),
            })

        return result

    def get_market(self, market_id: int) -> dict():
        url = self.gamma_markets_endpoint + "/" + str(market_id)
        print(url)
        response = httpx.get(url)
        return response.json()


if __name__ == "__main__":
    gamma = GammaMarketClient()
    market = gamma.get_market("253123")
    poly = Polymarket()
    object = poly.map_api_to_market(market)
