"""
Outcome Verifier for Resolution Scalper

Checks external free APIs to independently verify market outcomes before trading.
Supports crypto price markets (CoinGecko/Binance) and stock price markets (yfinance).

Returns:
- VERIFIED_YES: External data confirms the YES outcome
- VERIFIED_NO: External data confirms the NO outcome
- UNVERIFIABLE: Cannot independently verify (politics, sports, misc)
"""

import re
import logging
from enum import Enum
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class VerificationResult(Enum):
    VERIFIED_YES = "VERIFIED_YES"
    VERIFIED_NO = "VERIFIED_NO"
    UNVERIFIABLE = "UNVERIFIABLE"


# Common crypto asset name -> CoinGecko ID mapping
CRYPTO_IDS = {
    "btc": "bitcoin",
    "bitcoin": "bitcoin",
    "eth": "ethereum",
    "ethereum": "ethereum",
    "sol": "solana",
    "solana": "solana",
    "doge": "dogecoin",
    "dogecoin": "dogecoin",
    "xrp": "ripple",
    "ada": "cardano",
    "bnb": "binancecoin",
    "avax": "avalanche-2",
    "matic": "matic-network",
    "polygon": "matic-network",
    "dot": "polkadot",
    "link": "chainlink",
    "sui": "sui",
}

# Binance symbol mapping
CRYPTO_BINANCE = {
    "btc": "BTCUSDT",
    "bitcoin": "BTCUSDT",
    "eth": "ETHUSDT",
    "ethereum": "ETHUSDT",
    "sol": "SOLUSDT",
    "solana": "SOLUSDT",
    "doge": "DOGEUSDT",
    "xrp": "XRPUSDT",
    "ada": "ADAUSDT",
    "bnb": "BNBUSDT",
    "avax": "AVAXUSDT",
    "matic": "MATICUSDT",
    "dot": "DOTUSDT",
    "link": "LINKUSDT",
    "sui": "SUIUSDT",
}


class OutcomeVerifier:
    """Verifies market outcomes using free external APIs."""

    def __init__(self):
        self._http = httpx.Client(timeout=10)

    def verify(self, question: str, market_data: dict) -> VerificationResult:
        """
        Attempt to verify a market outcome independently.

        Args:
            question: The market question text
            market_data: Full market dict from Gamma API

        Returns:
            VerificationResult enum
        """
        q = question.lower().strip()

        # Try crypto verification first (most common verifiable type)
        crypto_result = self._try_verify_crypto(q, market_data)
        if crypto_result != VerificationResult.UNVERIFIABLE:
            return crypto_result

        # Try stock verification
        stock_result = self._try_verify_stock(q, market_data)
        if stock_result != VerificationResult.UNVERIFIABLE:
            return stock_result

        return VerificationResult.UNVERIFIABLE

    # ── Crypto Price Verification ──

    def _try_verify_crypto(self, question: str, market_data: dict) -> VerificationResult:
        """
        Verify crypto price markets.

        Patterns:
        - "Will BTC be above $70,000 on February 10?"
        - "Will Bitcoin close above $100k?"
        - "BTC above $95,000 on Feb 14?" (YES/NO)
        - "Will BTC hit $100,000 before March?"
        """
        parsed = self._parse_crypto_question(question)
        if parsed is None:
            return VerificationResult.UNVERIFIABLE

        asset, threshold, direction, target_date = parsed

        # Only verify if the target date has passed (outcome is determined)
        now = datetime.now(timezone.utc)
        if target_date and target_date > now:
            logger.debug(f"Crypto market target date {target_date} is in the future")
            return VerificationResult.UNVERIFIABLE

        # Get the price at the target date
        price = self._get_crypto_price(asset, target_date)
        if price is None:
            logger.debug(f"Could not fetch crypto price for {asset}")
            return VerificationResult.UNVERIFIABLE

        logger.info(f"Crypto verification: {asset} price=${price:,.2f}, threshold=${threshold:,.2f}, direction={direction}")

        if direction == "above":
            outcome_yes = price > threshold
        elif direction == "below":
            outcome_yes = price < threshold
        elif direction == "between":
            # threshold is (low, high) tuple — handled in parse
            return VerificationResult.UNVERIFIABLE
        else:
            return VerificationResult.UNVERIFIABLE

        if outcome_yes:
            return VerificationResult.VERIFIED_YES
        else:
            return VerificationResult.VERIFIED_NO

    def _parse_crypto_question(self, question: str) -> Optional[tuple]:
        """
        Parse crypto price question into (asset, threshold, direction, target_date).

        Returns None if not a crypto price market.
        """
        # Must mention a known crypto asset
        asset = None
        for name in CRYPTO_IDS:
            if name in question:
                asset = name
                break
        if asset is None:
            return None

        # Must have a price threshold
        # Match patterns like: $70,000  $70000  $100k  $100K  $1,234.56
        price_match = re.search(r'\$([0-9,]+(?:\.\d+)?)\s*([kKmM])?', question)
        if not price_match:
            return None

        price_str = price_match.group(1).replace(',', '')
        threshold = float(price_str)
        multiplier = price_match.group(2)
        if multiplier:
            if multiplier.lower() == 'k':
                threshold *= 1_000
            elif multiplier.lower() == 'm':
                threshold *= 1_000_000

        # Determine direction
        direction = "above"  # default
        if any(w in question for w in ["above", "over", "higher than", "more than", "exceed"]):
            direction = "above"
        elif any(w in question for w in ["below", "under", "lower than", "less than"]):
            direction = "below"
        elif "between" in question or "close at" in question:
            direction = "between"

        # Parse target date from question or endDate
        target_date = self._parse_date_from_question(question)

        return (asset, threshold, direction, target_date)

    def _parse_date_from_question(self, question: str) -> Optional[datetime]:
        """Try to extract a date from the question text, or return None."""
        now = datetime.now(timezone.utc)

        # Pattern: "on February 10" or "on Feb 10"
        date_match = re.search(
            r'on\s+(\w+)\s+(\d{1,2})(?:\s*,?\s*(\d{4}))?',
            question
        )
        if date_match:
            month_str = date_match.group(1)
            day = int(date_match.group(2))
            year = int(date_match.group(3)) if date_match.group(3) else now.year

            month_map = {
                'january': 1, 'jan': 1, 'february': 2, 'feb': 2,
                'march': 3, 'mar': 3, 'april': 4, 'apr': 4,
                'may': 5, 'june': 6, 'jun': 6, 'july': 7, 'jul': 7,
                'august': 8, 'aug': 8, 'september': 9, 'sep': 9, 'sept': 9,
                'october': 10, 'oct': 10, 'november': 11, 'nov': 11,
                'december': 12, 'dec': 12,
            }
            month = month_map.get(month_str.lower())
            if month:
                try:
                    # Use end of day UTC for the target date
                    return datetime(year, month, day, 23, 59, 59, tzinfo=timezone.utc)
                except ValueError:
                    pass

        return None

    def _get_crypto_price(self, asset: str, target_date: Optional[datetime] = None) -> Optional[float]:
        """
        Get crypto price from Binance (primary) or CoinGecko (fallback).

        For historical dates, uses Binance klines.
        For current price, uses Binance ticker.
        """
        # Try Binance first (higher rate limit)
        price = self._get_binance_price(asset, target_date)
        if price is not None:
            return price

        # Fallback to CoinGecko
        return self._get_coingecko_price(asset, target_date)

    def _get_binance_price(self, asset: str, target_date: Optional[datetime] = None) -> Optional[float]:
        """Get price from Binance REST API."""
        symbol = CRYPTO_BINANCE.get(asset)
        if not symbol:
            return None

        try:
            if target_date is None:
                # Current price
                resp = self._http.get(
                    "https://api.binance.com/api/v3/ticker/price",
                    params={"symbol": symbol}
                )
                if resp.status_code == 200:
                    return float(resp.json()["price"])
            else:
                # Historical: get the closing price kline for that day
                start_ms = int(target_date.replace(hour=0, minute=0, second=0).timestamp() * 1000)
                end_ms = int(target_date.timestamp() * 1000)
                resp = self._http.get(
                    "https://api.binance.com/api/v3/klines",
                    params={
                        "symbol": symbol,
                        "interval": "1d",
                        "startTime": start_ms,
                        "endTime": end_ms,
                        "limit": 1,
                    }
                )
                if resp.status_code == 200:
                    klines = resp.json()
                    if klines:
                        # kline[4] = close price
                        return float(klines[0][4])
        except Exception as e:
            logger.debug(f"Binance price fetch failed for {symbol}: {e}")

        return None

    def _get_coingecko_price(self, asset: str, target_date: Optional[datetime] = None) -> Optional[float]:
        """Get price from CoinGecko free API."""
        coin_id = CRYPTO_IDS.get(asset)
        if not coin_id:
            return None

        try:
            if target_date is None:
                resp = self._http.get(
                    "https://api.coingecko.com/api/v3/simple/price",
                    params={"ids": coin_id, "vs_currencies": "usd"}
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get(coin_id, {}).get("usd")
            else:
                # Historical price: CoinGecko uses DD-MM-YYYY format
                date_str = target_date.strftime("%d-%m-%Y")
                resp = self._http.get(
                    f"https://api.coingecko.com/api/v3/coins/{coin_id}/history",
                    params={"date": date_str, "localization": "false"}
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("market_data", {}).get("current_price", {}).get("usd")
        except Exception as e:
            logger.debug(f"CoinGecko price fetch failed for {coin_id}: {e}")

        return None

    # ── Stock Price Verification ──

    def _try_verify_stock(self, question: str, market_data: dict) -> VerificationResult:
        """
        Verify stock price markets.

        Patterns:
        - "Netflix up or down on Feb 5?"
        - "NFLX close above $900?"
        - "Will AAPL close at $90-$100 on Feb 10?"
        - "S&P 500 up or down on February 14?"
        """
        parsed = self._parse_stock_question(question)
        if parsed is None:
            return VerificationResult.UNVERIFIABLE

        ticker, condition, target_date = parsed

        # Only verify if the target date has passed
        now = datetime.now(timezone.utc)
        if target_date and target_date > now:
            return VerificationResult.UNVERIFIABLE

        # Get stock data
        close_price, prev_close = self._get_stock_prices(ticker, target_date)
        if close_price is None:
            logger.debug(f"Could not fetch stock price for {ticker}")
            return VerificationResult.UNVERIFIABLE

        logger.info(f"Stock verification: {ticker} close=${close_price:.2f}, prev_close=${prev_close:.2f if prev_close else 0}, condition={condition}")

        cond_type = condition.get("type")

        if cond_type == "up_or_down":
            if prev_close is None:
                return VerificationResult.UNVERIFIABLE
            went_up = close_price >= prev_close
            # "up or down" markets: YES = up, NO = down
            if went_up:
                return VerificationResult.VERIFIED_YES
            else:
                return VerificationResult.VERIFIED_NO

        elif cond_type == "above":
            threshold = condition["threshold"]
            if close_price > threshold:
                return VerificationResult.VERIFIED_YES
            else:
                return VerificationResult.VERIFIED_NO

        elif cond_type == "below":
            threshold = condition["threshold"]
            if close_price < threshold:
                return VerificationResult.VERIFIED_YES
            else:
                return VerificationResult.VERIFIED_NO

        elif cond_type == "between":
            low = condition["low"]
            high = condition["high"]
            if low <= close_price <= high:
                return VerificationResult.VERIFIED_YES
            else:
                return VerificationResult.VERIFIED_NO

        return VerificationResult.UNVERIFIABLE

    def _parse_stock_question(self, question: str) -> Optional[tuple]:
        """
        Parse stock question into (ticker, condition_dict, target_date).

        Returns None if not a stock market question.
        """
        # Known stock/index tickers and their yfinance symbols
        stock_patterns = {
            "netflix": "NFLX", "nflx": "NFLX",
            "apple": "AAPL", "aapl": "AAPL",
            "tesla": "TSLA", "tsla": "TSLA",
            "google": "GOOGL", "googl": "GOOGL", "alphabet": "GOOGL",
            "amazon": "AMZN", "amzn": "AMZN",
            "microsoft": "MSFT", "msft": "MSFT",
            "nvidia": "NVDA", "nvda": "NVDA",
            "meta": "META",
            "s&p 500": "^GSPC", "s&p": "^GSPC", "spy": "SPY",
            "dow jones": "^DJI", "dow": "^DJI",
            "nasdaq": "^IXIC",
        }

        ticker = None
        for name, sym in stock_patterns.items():
            if name in question:
                ticker = sym
                break

        if ticker is None:
            # Try to find a ticker symbol pattern (2-5 uppercase letters)
            ticker_match = re.search(r'\b([A-Z]{2,5})\b', question.upper())
            # Only use if it looks like a stock question
            if ticker_match and any(w in question for w in ["close", "stock", "trading day", "up or down"]):
                ticker = ticker_match.group(1)
            else:
                return None

        # Determine condition
        condition = {}
        if "up or down" in question:
            condition = {"type": "up_or_down"}
        elif "close above" in question or "above" in question:
            price_match = re.search(r'\$([0-9,]+(?:\.\d+)?)', question)
            if price_match:
                threshold = float(price_match.group(1).replace(',', ''))
                condition = {"type": "above", "threshold": threshold}
            else:
                return None
        elif "close below" in question or "below" in question:
            price_match = re.search(r'\$([0-9,]+(?:\.\d+)?)', question)
            if price_match:
                threshold = float(price_match.group(1).replace(',', ''))
                condition = {"type": "below", "threshold": threshold}
            else:
                return None
        elif "close at" in question or "close between" in question or "between" in question:
            range_match = re.search(r'\$([0-9,]+(?:\.\d+)?)\s*[-–]\s*\$?([0-9,]+(?:\.\d+)?)', question)
            if range_match:
                low = float(range_match.group(1).replace(',', ''))
                high = float(range_match.group(2).replace(',', ''))
                condition = {"type": "between", "low": low, "high": high}
            else:
                return None
        else:
            return None

        target_date = self._parse_date_from_question(question)
        return (ticker, condition, target_date)

    def _get_stock_prices(self, ticker: str, target_date: Optional[datetime] = None) -> tuple[Optional[float], Optional[float]]:
        """
        Get stock close price and previous close using yfinance.

        Returns (close_price, prev_close) or (None, None) on failure.
        """
        try:
            import yfinance as yf
        except ImportError:
            logger.debug("yfinance not installed, skipping stock verification")
            return None, None

        try:
            stock = yf.Ticker(ticker)

            if target_date is None:
                target_date = datetime.now(timezone.utc)

            # Fetch 5 days of data around the target date to get close + prev close
            start = (target_date - timedelta(days=7)).strftime("%Y-%m-%d")
            end = (target_date + timedelta(days=1)).strftime("%Y-%m-%d")

            hist = stock.history(start=start, end=end)
            if hist.empty or len(hist) < 1:
                return None, None

            # Get the close price on or before target date
            target_str = target_date.strftime("%Y-%m-%d")
            # Filter to dates <= target
            before_target = hist[hist.index.strftime("%Y-%m-%d") <= target_str]
            if before_target.empty:
                return None, None

            close_price = float(before_target.iloc[-1]["Close"])
            prev_close = float(before_target.iloc[-2]["Close"]) if len(before_target) >= 2 else None

            return close_price, prev_close

        except Exception as e:
            logger.debug(f"yfinance fetch failed for {ticker}: {e}")
            return None, None
