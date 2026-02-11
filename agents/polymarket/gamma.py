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

    def get_15min_crypto_markets(self, asset: str = "btc", num_windows: int = 8) -> list[dict]:
        """
        Fetch active 15-minute crypto up/down markets by constructing slugs.

        These markets are 'restricted' on Polymarket and don't appear in normal
        market listings. Instead we construct the event slug from the timestamp
        pattern: {asset}-updown-15m-{unix_timestamp}

        Args:
            asset: Crypto asset ("btc", "eth", "sol")
            num_windows: Number of 15-min windows ahead to check

        Returns:
            List of market dicts that are open and accepting orders
        """
        import time as _time
        from datetime import datetime, timezone

        asset_lower = asset.lower()
        now = datetime.now(timezone.utc)
        current_ts = int(now.timestamp())

        # Round down to nearest 15-min boundary (900 seconds)
        window_start = (current_ts // 900) * 900

        filtered = []

        # Check current window and several future windows
        for offset in range(num_windows):
            slug_ts = window_start + (offset * 900)
            slug = f"{asset_lower}-updown-15m-{slug_ts}"

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

                    filtered.append(market)

            except Exception:
                continue

        return filtered

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
