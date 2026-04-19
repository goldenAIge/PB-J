"""
Weather Temperature Trading Bot for Polymarket

Strategy: Exploit forecast accuracy vs. stale market odds on daily temperature markets.
- Polymarket has daily "Highest temperature in {City}?" markets for 12 cities
- Each market has 7 temperature brackets (e.g., "33°F or below", "34-35°F", ...)
- Poll Open-Meteo Ensemble API for 51-member ECMWF forecasts (FREE, no API key)
- Convert ensemble members into bracket probabilities
- When our probability > market price + edge threshold → buy the bracket
- Hold to resolution → collect $1.00 per share if correct

Key edge: Better probability estimates from ensemble weather models vs. stale market prices.
No LLM calls. No paid APIs. Pure math.

Usage:
    python -m agents.application.weather_trader --dry-run --max-iterations 5
    python -m agents.application.weather_trader --live --scan-interval 1800
"""

import os
import sys
import json
import time
import asyncio
import logging
import argparse
import re
from datetime import datetime, timezone, timedelta
from typing import Optional, NamedTuple
from dataclasses import dataclass, asdict

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from agents.polymarket.polymarket import Polymarket
from agents.polymarket.gamma import GammaMarketClient
from agents.application.risk_manager import RiskConfig, PortfolioRiskManager
from agents.connectors.telegram_alerts import TelegramAlerter

load_dotenv()

# Configure logging — FileHandler only to avoid pipe-blocking when run as background task.
# StreamHandler is added only when running interactively (see main()).
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('weather_trades.log'),
    ]
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# City definitions — lat/lon for Open-Meteo, matching Polymarket's 12 cities
# ---------------------------------------------------------------------------
CITIES = {
    "New York City": {"lat": 40.71, "lon": -74.01, "unit": "F", "aliases": ["NYC", "New York"]},
    "Chicago":       {"lat": 41.88, "lon": -87.63, "unit": "F", "aliases": []},
    "Dallas":        {"lat": 32.78, "lon": -96.80, "unit": "F", "aliases": []},
    "Atlanta":       {"lat": 33.75, "lon": -84.39, "unit": "F", "aliases": []},
    "Miami":         {"lat": 25.76, "lon": -80.19, "unit": "F", "aliases": []},
    "Seattle":       {"lat": 47.61, "lon": -122.33, "unit": "F", "aliases": []},
    "London":        {"lat": 51.51, "lon": -0.13,  "unit": "C", "aliases": []},
    "Toronto":       {"lat": 43.65, "lon": -79.38, "unit": "C", "aliases": []},
    "Buenos Aires":  {"lat": -34.60, "lon": -58.38, "unit": "C", "aliases": []},
    "Ankara":        {"lat": 39.93, "lon": 32.85,  "unit": "C", "aliases": []},
    "Seoul":         {"lat": 37.57, "lon": 126.98, "unit": "C", "aliases": []},
    "Wellington":    {"lat": -41.29, "lon": 174.78, "unit": "C", "aliases": []},
    "Paris":         {"lat": 48.86, "lon": 2.35,   "unit": "C", "aliases": []},
    "Sao Paulo":     {"lat": -23.55, "lon": -46.63, "unit": "C", "aliases": ["São Paulo"]},
}

# METAR airport station codes for observation-based trading
CITY_STATIONS = {
    "New York City": {"icao": "KLGA", "tz": "America/New_York"},  # LaGuardia — Polymarket resolution station
    "Chicago":       {"icao": "KORD", "tz": "America/Chicago"},
    "Dallas":        {"icao": "KDAL", "tz": "America/Chicago"},  # Love Field — Polymarket resolution station
    "Atlanta":       {"icao": "KATL", "tz": "America/New_York"},
    "Miami":         {"icao": "KMIA", "tz": "America/New_York"},
    "Seattle":       {"icao": "KSEA", "tz": "America/Los_Angeles"},
    "London":        {"icao": "EGLC", "tz": "Europe/London"},  # City Airport — Polymarket resolution station
    "Toronto":       {"icao": "CYYZ", "tz": "America/Toronto"},
    "Buenos Aires":  {"icao": "SAEZ", "tz": "America/Argentina/Buenos_Aires"},
    "Ankara":        {"icao": "LTAC", "tz": "Europe/Istanbul"},
    "Seoul":         {"icao": "RKSI", "tz": "Asia/Seoul"},
    "Wellington":    {"icao": "NZWN", "tz": "Pacific/Auckland"},
    "Paris":         {"icao": "LFPG", "tz": "Europe/Paris"},
    "Sao Paulo":     {"icao": "SBGR", "tz": "America/Sao_Paulo"},
}

# Cities where the daily high occurs BEFORE noon UTC resolution.
# HIGH CONFIDENCE: daily high has definitively passed by noon UTC.
# Seoul: high ~2-4pm KST (5-7am UTC), resolution noon UTC
# Wellington: high ~2-4pm NZDT (1-3am UTC), resolution noon UTC
OBSERVATION_HIGH_CONFIDENCE = {"Seoul", "Wellington"}

# NEAR-NOON: DISABLED — 1W/4L across Feb 22-23. Resolution consistently comes in
# 1°C higher than METAR observation. METAR doesn't update fast enough before noon UTC.
# Ankara, London, Paris all showed the same +1°C drift pattern.
OBSERVATION_NEAR_NOON: set[str] = set()  # disabled

# All observation-viable cities
OBSERVATION_VIABLE_CITIES = OBSERVATION_HIGH_CONFIDENCE | OBSERVATION_NEAR_NOON


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
class WeatherConfig(BaseModel):
    """Configuration for weather temperature trading"""
    # Edge thresholds
    min_edge: float = Field(default=0.08, description="Min edge to trade (our prob - market price)")
    max_edge: float = Field(default=0.40, description="Cap max edge — huge edges usually mean we're wrong")
    max_entry_price: float = Field(default=0.55, description="Don't buy brackets priced above $0.55")
    min_entry_price: float = Field(default=0.03, description="Skip near-zero brackets")
    min_model_confidence: float = Field(default=0.15, description="Our model must assign ≥15% prob")
    max_model_probability: float = Field(default=0.95, description="Cap model prob — no 100% certainty")

    # Market-informed skepticism
    market_blend_weight: float = Field(default=0.20, description="Blend 20% market price into our estimate")
    max_market_divergence: float = Field(default=0.50, description="Skip if model prob > market price + this")

    # Position sizing
    trade_percent: float = Field(default=0.03, description="3% of cash per trade")
    min_trade_size: float = Field(default=5.0, description="Polymarket minimum order is $5")
    max_trade_size: float = Field(default=10.0, description="Max trade size in USDC")
    max_concurrent_positions: int = Field(default=50, description="Max open positions at once")
    max_positions_per_city: int = Field(default=5, description="Allow more positions per city for data collection")

    # Timing
    scan_interval: float = Field(default=1800, description="30 min between scans")
    order_timeout: float = Field(default=300, description="Cancel unfilled orders after 5 min")
    min_hours_to_resolution: float = Field(default=2.0, description="Don't trade <2h before resolution")
    max_hours_to_resolution: float = Field(default=72.0, description="Don't trade >3 days out")

    # Weather data
    ensemble_models: list[str] = Field(
        default=["ecmwf_ifs025", "gfs_seamless"],
        description="Ensemble models to fetch (multi-model for cross-validation)"
    )
    forecast_days: int = Field(default=3, description="Days of forecast to fetch")
    min_model_agreement: float = Field(default=0.10, description="Both models must assign ≥10% to same bracket")
    max_model_divergence_c: float = Field(default=5.0, description="Skip if ensemble means differ by >5°C")
    kelly_fraction: float = Field(default=0.25, description="Quarter-Kelly for position sizing")

    # Time-based confidence scaling
    time_confidence_hours: float = Field(default=24.0, description="Full confidence within this many hours")
    time_confidence_floor: float = Field(default=0.60, description="Min confidence multiplier for distant forecasts")

    # Observation-based trading (METAR)
    observation_mode: bool = Field(default=True, description="Use METAR observations for near-resolution trading")
    obs_scan_interval: float = Field(default=600, description="10 min between scans in observation mode")
    obs_min_observations: int = Field(default=3, description="Minimum METAR reports to trust daily max")
    obs_boundary_buffer_f: float = Field(default=0.5, description="Skip if temp within this many F of bracket edge")
    obs_boundary_buffer_c: float = Field(default=0.3, description="Skip if temp within this many C of bracket edge")
    obs_min_edge: float = Field(default=0.15, description="Higher min edge for observation trades")
    obs_max_entry_price: float = Field(default=0.70, description="Can enter at higher prices with observation confidence")
    obs_min_hours_to_resolution: float = Field(default=0.5, description="Trade up to 30 min before resolution")
    obs_max_hours_to_resolution: float = Field(default=6.0, description="Only trade within 6h of resolution (Seoul peaks ~7h before)")
    obs_trade_percent: float = Field(default=0.10, description="10% of balance per observation trade")
    obs_max_trade_size: float = Field(default=25.0, description="Higher max for observation trades")

    # Near-noon observation (London/Paris) — looser params since high may not be final
    near_noon_max_hours: float = Field(default=1.0, description="Only trade within 1h of resolution (temps stabilize ~0.7-1h before)")
    near_noon_buffer_c: float = Field(default=0.3, description="Buffer for near-noon cities (0.3°C allows integer temps on 1°C brackets)")
    near_noon_max_confidence: float = Field(default=0.65, description="Lower confidence cap for near-noon cities")

    # Static EV trading — buy cheap brackets with high model probability
    ev_mode: bool = Field(default=False, description="Enable static EV trading (DISABLED — 0/15 track record)")
    ev_max_entry_price: float = Field(default=0.05, description="Only buy brackets under $0.05")
    ev_min_model_prob: float = Field(default=0.05, description="Model must give >=5% probability")
    ev_min_multiple: float = Field(default=3.0, description="Require 3x EV (prob/price)")
    ev_trade_size: float = Field(default=5.0, description="$5 per EV trade (lottery-style)")
    ev_max_positions_per_event: int = Field(default=1, description="Max 1 EV trade per city+date")
    ev_refresh_interval: float = Field(default=1800, description="Refresh forecasts every 30 min")

    # Basket sizing — buy multiple adjacent brackets per event
    basket_mode: bool = Field(default=True, description="Enable basket trading across adjacent brackets")
    basket_max_legs: int = Field(default=5, description="Max brackets per basket")
    basket_min_legs: int = Field(default=2, description="Require at least 2 legs (otherwise not a basket)")
    basket_total_percent: float = Field(default=0.15, description="15% of balance per basket")
    basket_max_total: float = Field(default=30.0, description="Max total basket spend (suitable for ~$200 balance)")
    basket_min_leg_size: float = Field(default=5.0, description="Polymarket minimum order size")
    basket_min_leg_edge: float = Field(default=0.03, description="Lower per-leg edge threshold (diversified risk)")
    basket_max_leg_price: float = Field(default=0.40, description="Max entry price per leg")
    basket_min_prob: float = Field(default=0.05, description="Min model probability for a leg")
    basket_scan_interval: float = Field(default=1800, description="30 min between scans (forecast-paced)")
    basket_forecast_refresh: float = Field(default=1800, description="Refresh ensemble forecasts every 30 min")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
class CityForecast(NamedTuple):
    """Combined multi-model ensemble forecast for one city on one date"""
    city: str
    date: str                       # "2026-02-13"
    ensemble_temps_c: list[float]   # ALL members from ALL models combined (e.g. 82 total)
    model_temps_c: dict             # Per-model temps: {"ecmwf_ifs025": [...], "gfs_seamless": [...]}
    fetched_at: float               # Unix timestamp


@dataclass
class WeatherSignal:
    """A trading signal for a weather temperature bracket"""
    event_id: str
    market_id: str
    city: str
    date: str
    bracket: str            # Human-readable, e.g. "36-37°F"
    bracket_low: float
    bracket_high: float
    token_id: str
    model_prob: float       # Our probability from ensemble
    market_price: float     # Current Polymarket YES price
    edge: float             # model_prob - market_price
    hours_remaining: float
    ensemble_agree: int     # How many of 51 members land in this bracket
    source: str = "forecast"  # "forecast", "observation", "near_noon_obs", "ev", or "basket"
    basket_size: Optional[float] = None  # Pre-calculated trade size for basket legs


@dataclass
class WeatherTrade:
    """Record of an executed weather trade"""
    timestamp: str
    event_id: str
    market_id: str
    city: str
    date: str
    bracket: str
    amount: float
    entry_price: float
    expected_payout: float
    expected_profit: float
    model_prob: float
    market_price_at_entry: float
    edge_at_entry: float
    ensemble_agree: int
    hours_remaining: float
    status: str             # PENDING, FILLED, DRY_RUN, TIMEOUT_CANCELLED, FAILED
    # Resolution
    order_id: Optional[str] = None
    end_date: Optional[str] = None
    resolved_bracket: Optional[str] = None
    actual_profit: Optional[float] = None
    resolution_status: str = "pending"  # "pending", "win", "loss", "unknown"


# ---------------------------------------------------------------------------
# Weather Data Feed — Open-Meteo Multi-Model Ensemble API
# ---------------------------------------------------------------------------
class WeatherDataFeed:
    """
    Polls Open-Meteo Ensemble API for daily high temp forecasts from
    MULTIPLE models (ECMWF + GFS) and combines them for robust probability
    estimates. Completely free, no API key needed.

    ECMWF IFS 0.25°: 51 members (1 control + 50 perturbed) — best global model
    GFS Seamless:     31 members (1 control + 30 perturbed) — good second opinion

    Combined: 82 members total, reducing single-model bias.

    CRITICAL: Polymarket resolves using Weather Underground station data, NOT
    reanalysis/model data. Historical validation shows ~2.1°F mean absolute
    error between Open-Meteo and Weather Underground. This class applies
    Gaussian smoothing to each ensemble member to account for this uncertainty.
    """

    ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"

    # Member count per model (control + perturbed)
    MODEL_MEMBERS = {
        "ecmwf_ifs025": 51,   # member01-member50 + control
        "gfs_seamless": 31,   # member01-member30 + control
        "icon_seamless": 40,  # member01-member39 + control
    }

    # Per-city bias correction: Open-Meteo tends to read this many °F/°C
    # above/below Weather Underground's resolution value.
    # Negative bias = Open-Meteo reads COOLER than WU → we shift forecasts warmer.
    # Updated 2026-02-14 based on 8 resolved trades: ALL misses were warm.
    # Universal cold bias confirmed — Open-Meteo systematically underestimates highs.
    CITY_BIAS = {
        # bias_degrees is in the city's native unit (F or C)
        # sigma_degrees is the measurement uncertainty std dev
        "New York City": {"bias": -1.5, "sigma": 3.0},   # Historical -0.4, widened from dry-run data
        "Chicago":       {"bias": -1.5, "sigma": 3.0},   # Dry-run: missed by +3°F warm
        "Dallas":        {"bias": -1.0, "sigma": 3.0},
        "Atlanta":       {"bias": -1.0, "sigma": 3.0},
        "Miami":         {"bias": -1.0, "sigma": 3.0},
        "Seattle":       {"bias": -1.0, "sigma": 3.0},
        "London":        {"bias": 0.0,  "sigma": 2.5},   # Was +0.6 but dry-run showed WU warmer; reset to neutral
        "Toronto":       {"bias": -0.5, "sigma": 2.0},
        "Buenos Aires":  {"bias": -0.5, "sigma": 2.0},
        "Ankara":        {"bias": -1.0, "sigma": 2.5},   # Dry-run: missed by +2-3°C warm
        "Seoul":         {"bias": -0.5, "sigma": 2.0},   # Dry-run: missed by +1°C warm
        "Wellington":    {"bias": -0.5, "sigma": 2.0},
        "Paris":         {"bias": 0.0,  "sigma": 2.5},
        "Sao Paulo":     {"bias": -0.5, "sigma": 2.0},
    }

    def __init__(self, models: list[str] = None, forecast_days: int = 3):
        self._models = models or ["ecmwf_ifs025", "gfs_seamless"]
        self._forecast_days = forecast_days
        self._cache: dict[str, CityForecast] = {}
        self._last_fetch: float = 0

    async def update(self) -> int:
        """
        Fetch ensemble forecasts from ALL configured models for all cities.
        One API call per model, then combine members into a single forecast.
        Returns the number of city-date forecasts cached.
        """
        cities_list = list(CITIES.items())
        lats = ",".join(str(info["lat"]) for _, info in cities_list)
        lons = ",".join(str(info["lon"]) for _, info in cities_list)

        # Fetch each model separately (different member counts)
        # model_data[model_name][city_idx] = {date: [temps]}
        model_data: dict[str, dict[int, dict[str, list[float]]]] = {}

        async with httpx.AsyncClient(timeout=30) as client:
            for model_name in self._models:
                max_members = self.MODEL_MEMBERS.get(model_name, 51)
                params = {
                    "latitude": lats,
                    "longitude": lons,
                    "daily": "temperature_2m_max",
                    "models": model_name,
                    "forecast_days": self._forecast_days,
                    "temperature_unit": "celsius",
                }

                try:
                    response = await client.get(self.ENSEMBLE_URL, params=params)
                    if response.status_code != 200:
                        logger.warning(f"Open-Meteo {model_name} error: HTTP {response.status_code}")
                        continue

                    data = response.json()
                    if isinstance(data, dict):
                        data = [data]

                    model_data[model_name] = {}

                    for i, city_data in enumerate(data):
                        daily = city_data.get("daily", {})
                        dates = daily.get("time", [])

                        # Collect member keys for this model
                        member_keys = ["temperature_2m_max"]
                        for m in range(1, max_members):
                            member_keys.append(f"temperature_2m_max_member{m:02d}")

                        city_dates = {}
                        for day_idx, date_str in enumerate(dates):
                            temps = []
                            for key in member_keys:
                                series = daily.get(key, [])
                                if day_idx < len(series) and series[day_idx] is not None:
                                    temps.append(series[day_idx])
                            if temps:
                                city_dates[date_str] = temps
                        model_data[model_name][i] = city_dates

                    logger.info(f"  Fetched {model_name}: {len(data)} cities")

                except Exception as e:
                    logger.warning(f"Failed to fetch {model_name}: {e}")
                    continue

        if not model_data:
            logger.error("No ensemble data fetched from any model")
            return 0

        # Combine all models into unified forecasts
        now = time.time()
        count = 0

        for i, (city_name, _) in enumerate(cities_list):
            # Collect all dates across models
            all_dates = set()
            for model_name in model_data:
                if i in model_data[model_name]:
                    all_dates.update(model_data[model_name][i].keys())

            for date_str in sorted(all_dates):
                combined_temps = []
                per_model_temps = {}

                for model_name in model_data:
                    if i in model_data[model_name]:
                        temps = model_data[model_name][i].get(date_str, [])
                        if temps:
                            per_model_temps[model_name] = temps
                            combined_temps.extend(temps)

                if not combined_temps:
                    continue

                cache_key = f"{city_name}_{date_str}"
                self._cache[cache_key] = CityForecast(
                    city=city_name,
                    date=date_str,
                    ensemble_temps_c=combined_temps,
                    model_temps_c=per_model_temps,
                    fetched_at=now,
                )
                count += 1

        self._last_fetch = now
        total_members = sum(len(f.ensemble_temps_c) for f in self._cache.values()) // max(count, 1)
        logger.info(f"Weather feed updated: {count} city-date forecasts cached (~{total_members} members each)")
        return count

    def get_forecast(self, city: str, date: str) -> Optional[CityForecast]:
        """Look up cached forecast for a city+date."""
        return self._cache.get(f"{city}_{date}")

    def get_bracket_probabilities(
        self, city: str, date: str, brackets: list[dict], unit: str
    ) -> list[float]:
        """
        Convert combined ensemble members into bracket probabilities
        WITH resolution uncertainty smoothing.

        Instead of putting each ensemble member into exactly one bracket,
        we model it as a Gaussian centered on the member's temperature with
        sigma = measurement uncertainty between Open-Meteo and Weather
        Underground (~2.1°F for US cities, ~1.5°C for metric cities).

        This accounts for the fact that Polymarket resolves using WU data,
        not the same source as our forecasts.
        """
        from math import erf, sqrt

        forecast = self.get_forecast(city, date)
        if not forecast:
            return []

        temps = forecast.ensemble_temps_c
        if unit == "F":
            temps = [t * 9.0 / 5.0 + 32.0 for t in temps]

        n = len(temps)
        if n == 0:
            return []

        # Get city-specific bias and uncertainty
        city_info = self.CITY_BIAS.get(city, {"bias": 0.0, "sigma": 2.5})
        bias = city_info["bias"]
        sigma = city_info["sigma"]

        # Apply bias correction: shift temps toward WU expected value
        # If Open-Meteo reads 0.4°F cooler than WU, we add 0.4°F to our forecast
        temps = [t - bias for t in temps]

        # Gaussian CDF helper
        def norm_cdf(x, mu, s):
            return 0.5 * (1.0 + erf((x - mu) / (s * sqrt(2.0))))

        probs = []
        for bracket in brackets:
            low = bracket["bracket_low"]
            high = bracket["bracket_high"]

            # For each ensemble member, compute the probability that
            # the WU measurement lands in this bracket
            bracket_prob = 0.0
            for t in temps:
                p_high = norm_cdf(high, t, sigma) if high < float("inf") else 1.0
                p_low = norm_cdf(low, t, sigma) if low > float("-inf") else 0.0
                bracket_prob += (p_high - p_low)

            probs.append(bracket_prob / n)

        return probs

    def get_per_model_probabilities(
        self, city: str, date: str, brackets: list[dict], unit: str
    ) -> dict[str, list[float]]:
        """
        Get bracket probabilities from each model separately,
        with resolution uncertainty smoothing.
        Returns dict: model_name -> [prob_per_bracket].
        """
        from math import erf, sqrt

        forecast = self.get_forecast(city, date)
        if not forecast:
            return {}

        city_info = self.CITY_BIAS.get(city, {"bias": 0.0, "sigma": 2.5})
        bias = city_info["bias"]
        sigma = city_info["sigma"]

        def norm_cdf(x, mu, s):
            return 0.5 * (1.0 + erf((x - mu) / (s * sqrt(2.0))))

        result = {}
        for model_name, model_temps in forecast.model_temps_c.items():
            temps = model_temps
            if unit == "F":
                temps = [t * 9.0 / 5.0 + 32.0 for t in temps]

            # Apply bias correction
            temps = [t - bias for t in temps]

            n = len(temps)
            if n == 0:
                continue

            probs = []
            for bracket in brackets:
                low = bracket["bracket_low"]
                high = bracket["bracket_high"]
                bracket_prob = 0.0
                for t in temps:
                    p_high = norm_cdf(high, t, sigma) if high < float("inf") else 1.0
                    p_low = norm_cdf(low, t, sigma) if low > float("-inf") else 0.0
                    bracket_prob += (p_high - p_low)
                probs.append(bracket_prob / n)
            result[model_name] = probs

        return result

    def get_model_means_c(self, city: str, date: str) -> dict[str, float]:
        """Get the mean forecast temperature (°C) from each model."""
        forecast = self.get_forecast(city, date)
        if not forecast:
            return {}
        return {
            model: sum(temps) / len(temps)
            for model, temps in forecast.model_temps_c.items()
            if temps
        }


# ---------------------------------------------------------------------------
# Observation Data Feed — METAR Airport Weather Stations
# ---------------------------------------------------------------------------
class ObservationDataFeed:
    """
    Fetches actual temperature observations from METAR airport weather stations
    via the Aviation Weather API. Free, worldwide, updated every 20-60 min.

    Used for near-resolution trading: observe the actual daily high, identify
    which bracket it falls in, and buy that bracket before Polymarket resolves.
    """

    METAR_URL = "https://aviationweather.gov/api/data/metar"

    def __init__(self):
        # {city: {date_str: {"max_temp_c": float, "n_obs": int, "latest_obs": str}}}
        self._cache: dict[str, dict[str, dict]] = {}
        self._last_fetch: float = 0

    async def update(self) -> int:
        """
        Fetch last 24h of METAR observations for all 12 cities.
        Returns total number of city-date entries updated.
        """
        from zoneinfo import ZoneInfo

        all_icaos = ",".join(info["icao"] for info in CITY_STATIONS.values())

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(self.METAR_URL, params={
                    "ids": all_icaos,
                    "format": "json",
                    "hours": 24,
                })
                if response.status_code != 200:
                    logger.warning(f"METAR API error: HTTP {response.status_code}")
                    return 0

                observations = response.json()
                if not isinstance(observations, list):
                    logger.warning(f"METAR API returned unexpected format: {type(observations)}")
                    return 0

        except Exception as e:
            logger.warning(f"Failed to fetch METAR data: {e}")
            return 0

        # Build ICAO -> city mapping
        icao_to_city = {info["icao"]: city for city, info in CITY_STATIONS.items()}

        # Group observations by city and local date, tracking max temp
        city_dates: dict[str, dict[str, dict]] = {}
        for obs in observations:
            icao = obs.get("icaoId", "")
            city = icao_to_city.get(icao)
            if not city:
                continue

            temp_c = self._parse_metar_temp(obs)
            if temp_c is None:
                continue

            obs_time = obs.get("obsTime") or obs.get("reportTime", "")
            tz_name = CITY_STATIONS[city]["tz"]
            local_date = self._local_date(obs_time, tz_name)
            if not local_date:
                continue

            if city not in city_dates:
                city_dates[city] = {}
            if local_date not in city_dates[city]:
                city_dates[city][local_date] = {
                    "max_temp_c": temp_c,
                    "n_obs": 0,
                    "latest_obs": obs_time,
                }

            entry = city_dates[city][local_date]
            entry["n_obs"] += 1
            if temp_c > entry["max_temp_c"]:
                entry["max_temp_c"] = temp_c
            if obs_time > entry["latest_obs"]:
                entry["latest_obs"] = obs_time

        self._cache = city_dates
        self._last_fetch = time.time()

        total = sum(len(dates) for dates in city_dates.values())
        cities_with_data = len(city_dates)
        logger.info(f"METAR observations updated: {total} city-date entries across {cities_with_data} cities")

        return total

    def get_daily_max(self, city: str, date: str) -> Optional[tuple[float, int]]:
        """
        Get the observed daily max temp (°C) and observation count for a city+date.
        Returns (max_temp_c, n_observations) or None if no data.
        """
        city_data = self._cache.get(city, {})
        entry = city_data.get(date)
        if entry is None:
            return None
        return (entry["max_temp_c"], entry["n_obs"])

    def _parse_metar_temp(self, obs: dict) -> Optional[float]:
        """Extract temperature in Celsius from METAR JSON observation."""
        # The Aviation Weather API returns 'temp' in Celsius directly
        temp = obs.get("temp")
        if temp is not None:
            try:
                return float(temp)
            except (ValueError, TypeError):
                pass
        return None

    def _local_date(self, obs_time: str, tz_name: str) -> Optional[str]:
        """Convert observation time to local date string (YYYY-MM-DD)."""
        from zoneinfo import ZoneInfo

        if not obs_time:
            return None
        try:
            # METAR obsTime is typically ISO format or Unix timestamp
            if isinstance(obs_time, (int, float)):
                dt = datetime.fromtimestamp(obs_time, tz=timezone.utc)
            else:
                # Try ISO format first
                obs_time_str = str(obs_time)
                if "T" in obs_time_str or "-" in obs_time_str:
                    dt = datetime.fromisoformat(obs_time_str.replace("Z", "+00:00"))
                else:
                    # Unix timestamp as string
                    dt = datetime.fromtimestamp(float(obs_time_str), tz=timezone.utc)

            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)

            local_dt = dt.astimezone(ZoneInfo(tz_name))
            return local_dt.strftime("%Y-%m-%d")
        except Exception as e:
            logger.debug(f"Failed to parse obs time '{obs_time}': {e}")
            return None


# ---------------------------------------------------------------------------
# Weather Trader Bot
# ---------------------------------------------------------------------------
class WeatherTrader:
    """
    Weather Temperature Trading Bot for Polymarket.

    Exploits ensemble weather forecast accuracy vs. stale market odds
    on daily high temperature prediction markets.
    """

    def __init__(
        self,
        config: Optional[WeatherConfig] = None,
        risk_config: Optional[RiskConfig] = None,
        dry_run: bool = True,
        simulated_balance: Optional[float] = None,
    ):
        self.config = config or WeatherConfig()
        self.dry_run = dry_run
        self.simulated_balance = simulated_balance

        # Core services
        self.polymarket = Polymarket()
        self.gamma = GammaMarketClient()
        self.risk_config = risk_config or RiskConfig()
        self.risk_manager = PortfolioRiskManager(self.polymarket, self.risk_config)
        self.telegram = TelegramAlerter()

        # Weather data feed (multi-model)
        self.weather_feed = WeatherDataFeed(
            models=self.config.ensemble_models,
            forecast_days=self.config.forecast_days,
        )

        # Observation data feed (METAR)
        self.obs_feed = ObservationDataFeed()

        # Trading state
        self.traded_markets: set[str] = set()
        self.traded_events: set[str] = set()  # "city|date" keys to prevent multiple bets per event
        self.city_position_count: dict[str, int] = {}
        self.position_count: int = 0
        self.total_trades: int = 0
        self.successful_trades: int = 0
        self.failed_trades: int = 0
        self.trade_history: list[WeatherTrade] = []
        self.initial_balance: Optional[float] = None
        self.simulated_pnl: float = 0.0

        # Resolution verification
        self.pending_resolutions: list[WeatherTrade] = []
        self.verified_wins: int = 0
        self.verified_losses: int = 0
        self.verified_pnl: float = 0.0

        # Forecast-shift detection cache
        # Key: (city, date, bracket_label) -> previous model probability
        self._prev_forecast_probs: dict[tuple[str, str, str], float] = {}
        self._last_forecast_fetch: float = 0

        logger.info(f"WeatherTrader initialized (dry_run={dry_run})")
        logger.info(f"Config: {self.config.model_dump()}")

    def get_balance(self) -> float:
        if self.simulated_balance is not None:
            return self.simulated_balance + self.simulated_pnl
        return self.risk_manager.get_balance()

    def _match_city(self, event_city: str) -> Optional[str]:
        """Match a city name from Polymarket event to our CITIES dict."""
        # Direct match
        if event_city in CITIES:
            return event_city
        # Alias match
        event_lower = event_city.lower()
        for city_name, info in CITIES.items():
            if event_lower == city_name.lower():
                return city_name
            for alias in info.get("aliases", []):
                if event_lower == alias.lower():
                    return city_name
        return None

    def _derive_weather_date(self, event: dict) -> Optional[str]:
        """
        Extract the actual weather observation date from the event title.

        Event titles look like: "Highest temperature in New York City on February 13?"
        The endDate field is noon UTC on the day AFTER the weather date, so we
        parse the date from the title instead.
        """
        title = event.get("title", "")
        match = re.match(
            r"Highest temperature in .+? on (\w+ \d+)\??",
            title,
            re.IGNORECASE,
        )
        if not match:
            return None

        date_part = match.group(1)  # e.g. "February 13"

        # Use endDate year for disambiguation
        end_date_str = event.get("endDate", "")
        try:
            end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            year = end_dt.year
        except Exception:
            year = datetime.now(timezone.utc).year

        try:
            dt = datetime.strptime(f"{date_part} {year}", "%B %d %Y")
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            return None

    def evaluate_event_observation(self, event: dict) -> list[WeatherSignal]:
        """
        Evaluate a weather event using actual METAR observations (near-resolution).

        Two tiers:
        - HIGH CONFIDENCE (Seoul, Wellington, Ankara): daily high has definitively
          passed by noon UTC. Standard buffers/confidence.
        - NEAR-NOON (London, Paris): noon UTC ≈ local midday. Daily high may
          still rise ~1-2°C. Wider buffers, tighter time window, lower confidence.
        """
        city_raw = event.get("_city", "")
        city = self._match_city(city_raw)
        if not city:
            return []

        # Only evaluate cities where observation timing works
        if city not in OBSERVATION_VIABLE_CITIES:
            return []

        unit = event.get("_unit", "C")
        brackets = event.get("_brackets", [])
        if not brackets:
            return []

        # Derive the actual weather date from title
        weather_date = self._derive_weather_date(event)
        if not weather_date:
            weather_date = event.get("_date", "")
        if not weather_date:
            return []

        # Check hours to resolution
        end_date_str = event.get("endDate", "")
        try:
            end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        except Exception:
            return []

        now = datetime.now(timezone.utc)
        hours_remaining = (end_dt - now).total_seconds() / 3600.0
        is_near_noon = city in OBSERVATION_NEAR_NOON

        # Near-noon cities get a tighter time window (trade only in last 2h)
        max_hours = self.config.near_noon_max_hours if is_near_noon else self.config.obs_max_hours_to_resolution

        if hours_remaining < self.config.obs_min_hours_to_resolution:
            logger.debug(f"  [OBS] {city} {weather_date}: {hours_remaining:.1f}h left < {self.config.obs_min_hours_to_resolution}h min — too close to resolution")
            return []
        if hours_remaining > max_hours:
            tag = "NEAR-NOON" if is_near_noon else "OBS"
            logger.info(f"  [{tag}] {city} {weather_date}: {hours_remaining:.1f}h left > {max_hours:.1f}h max — not in observation window yet")
            return []

        # Get observed daily max from METAR
        obs_result = self.obs_feed.get_daily_max(city, weather_date)
        if obs_result is None:
            logger.info(f"  [OBS] {city} {weather_date} ({hours_remaining:.1f}h left): No METAR data yet")
            return []

        max_temp_c, n_obs = obs_result

        if n_obs < self.config.obs_min_observations:
            logger.info(
                f"  [OBS] {city} {weather_date} ({hours_remaining:.1f}h left): "
                f"Only {n_obs} obs (need {self.config.obs_min_observations})"
            )
            return []

        # Convert observed temp to city's unit
        if unit == "F":
            observed_temp = max_temp_c * 9.0 / 5.0 + 32.0
            # Near-noon F buffer: convert near_noon_buffer_c to F (×1.8)
            buffer = (self.config.near_noon_buffer_c * 1.8) if is_near_noon else self.config.obs_boundary_buffer_f
        else:
            observed_temp = max_temp_c
            buffer = self.config.near_noon_buffer_c if is_near_noon else self.config.obs_boundary_buffer_c

        # Find which bracket the observed temp falls into
        matching_bracket = None
        for bracket in brackets:
            low = bracket["bracket_low"]
            high = bracket["bracket_high"]
            if low <= observed_temp < high:
                matching_bracket = bracket
                break
            if observed_temp == high and high == float("inf"):
                matching_bracket = bracket
                break

        if matching_bracket is None:
            logger.warning(
                f"  [OBS] {city} {weather_date}: {observed_temp:.1f}°{unit} doesn't match any bracket!"
            )
            return []

        low = matching_bracket["bracket_low"]
        high = matching_bracket["bracket_high"]
        label = matching_bracket["group_item_title"]
        market_price = matching_bracket["yes_price"]

        # === PRICE DISCOVERY LOG ===
        # Log ALL bracket prices so we can see if the market is efficient.
        tag = "NEAR-NOON" if is_near_noon else "OBS"
        logger.info(
            f"  [{tag}] {city} {weather_date} ({hours_remaining:.1f}h left): "
            f"METAR max={observed_temp:.1f}°{unit} ({n_obs} obs) -> {label}"
        )
        for b in brackets:
            marker = " <<<" if b["market_id"] == matching_bracket["market_id"] else ""
            logger.info(
                f"    {b['group_item_title']:20s} ${b['yes_price']:.3f}{marker}"
            )

        # Boundary buffer check
        dist_to_low = observed_temp - low if low > float("-inf") else float("inf")
        dist_to_high = high - observed_temp if high < float("inf") else float("inf")
        min_dist = min(dist_to_low, dist_to_high)

        if min_dist < buffer:
            logger.info(
                f"    SKIP: {observed_temp:.1f}° only {min_dist:.1f}° from bracket edge (need {buffer:.1f}°)"
            )
            return []

        # Confidence scaling
        # METAR airport vs Weather Underground can disagree by ~0.5°C/1°F.
        # High-confidence cities: daily high is past, 60-80% model prob.
        # Near-noon cities: daily high may still rise, cap at 65%.
        obs_confidence = min(1.0, n_obs / 12.0)
        if low > float("-inf") and high < float("inf"):
            bracket_width = high - low
            center = (low + high) / 2.0
            centrality = 1.0 - abs(observed_temp - center) / (bracket_width / 2.0)
        else:
            centrality = 0.8

        if is_near_noon:
            # Lower base + lower cap: temp may still rise 1-2°C
            model_prob = 0.45 + 0.20 * obs_confidence * centrality
            model_prob = min(model_prob, self.config.near_noon_max_confidence)
        else:
            model_prob = 0.60 + 0.20 * obs_confidence * centrality
            model_prob = min(model_prob, 0.80)
        edge = model_prob - market_price

        logger.info(
            f"    SIGNAL: prob={model_prob:.0%} vs market=${market_price:.3f} | edge={edge:+.3f}"
        )

        # Skip already-traded markets or events (prevent multiple bets per city+date)
        event_key = f"{city}|{weather_date}"
        if event_key in self.traded_events:
            logger.info(f"    SKIP: already traded {city} {weather_date} (different bracket)")
            return []
        if matching_bracket["market_id"] in self.traded_markets:
            logger.info(f"    SKIP: already traded this market")
            return []

        if edge < self.config.obs_min_edge:
            logger.info(f"    SKIP: edge {edge:+.3f} below min {self.config.obs_min_edge}")
            return []
        if market_price > self.config.obs_max_entry_price:
            return []
        # No min_entry_price filter for observation trades — when we've observed
        # the actual daily high, even $0.01 entries are legitimate (high EV).

        signal = WeatherSignal(
            event_id=str(event.get("id", "")),
            market_id=matching_bracket["market_id"],
            city=city,
            date=weather_date,
            bracket=label,
            bracket_low=low,
            bracket_high=high,
            token_id=matching_bracket["yes_token_id"],
            model_prob=model_prob,
            market_price=market_price,
            edge=edge,
            hours_remaining=hours_remaining,
            ensemble_agree=n_obs,
            source="near_noon_obs" if is_near_noon else "observation",
        )

        return [signal]

    def evaluate_event(self, event: dict) -> list[WeatherSignal]:
        """
        Evaluate a single city+date temperature event for trading signals.

        Multi-model validation:
        1. Get combined probability from ALL ensemble members
        2. Get per-model probabilities for cross-validation
        3. Check model agreement — both models must assign meaningful prob
        4. Check model mean temperature divergence — skip if models disagree too much
        5. Use CONSERVATIVE (minimum) per-model probability for edge calculation
        """
        city_raw = event.get("_city", "")
        city = self._match_city(city_raw)
        if not city:
            logger.debug(f"Unknown city: {city_raw}")
            return []

        date = event.get("_date", "")
        unit = event.get("_unit", "C")
        brackets = event.get("_brackets", [])

        if not brackets:
            return []

        # Check hours to resolution
        end_date_str = event.get("endDate", "")
        try:
            end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        except Exception:
            return []

        now = datetime.now(timezone.utc)
        hours_remaining = (end_dt - now).total_seconds() / 3600.0

        if hours_remaining < self.config.min_hours_to_resolution:
            return []
        if hours_remaining > self.config.max_hours_to_resolution:
            return []

        # Get combined probability from all ensemble members
        combined_probs = self.weather_feed.get_bracket_probabilities(city, date, brackets, unit)
        if not combined_probs:
            logger.debug(f"  {city} {date}: No forecast data available")
            return []

        # Get per-model probabilities for cross-validation
        per_model_probs = self.weather_feed.get_per_model_probabilities(city, date, brackets, unit)
        model_means = self.weather_feed.get_model_means_c(city, date)

        forecast = self.weather_feed.get_forecast(city, date)
        n_members = len(forecast.ensemble_temps_c)

        # Check model mean divergence
        if len(model_means) >= 2:
            mean_vals = list(model_means.values())
            divergence = max(mean_vals) - min(mean_vals)
            if divergence > self.config.max_model_divergence_c:
                logger.info(f"  {city} {date}: SKIP — model means diverge by {divergence:.1f}°C (max {self.config.max_model_divergence_c}°C)")
                for name, mean in model_means.items():
                    logger.info(f"    {name}: mean={mean:.1f}°C")
                return []

        # Log model means
        means_str = " | ".join(f"{n}={m:.1f}°C" for n, m in model_means.items())
        logger.info(f"  {city} {date} ({hours_remaining:.1f}h left) [{means_str}]:")

        # Time-based confidence: full confidence within 24h, scaled down for distant forecasts
        if hours_remaining <= self.config.time_confidence_hours:
            time_confidence = 1.0
        else:
            # Linear decay from 1.0 to floor
            ratio = (hours_remaining - self.config.time_confidence_hours) / (
                self.config.max_hours_to_resolution - self.config.time_confidence_hours
            )
            time_confidence = max(
                self.config.time_confidence_floor,
                1.0 - ratio * (1.0 - self.config.time_confidence_floor),
            )

        signals = []

        for i, bracket in enumerate(brackets):
            combined_prob = combined_probs[i]
            market_price = bracket["yes_price"]
            label = bracket["group_item_title"]

            # Get the MINIMUM probability across all models (conservative estimate)
            min_model_prob = combined_prob
            model_details = []
            all_models_agree = True
            for model_name, model_probs_list in per_model_probs.items():
                mp = model_probs_list[i] if i < len(model_probs_list) else 0.0
                model_details.append(f"{model_name.split('_')[0]}={mp:.0%}")
                min_model_prob = min(min_model_prob, mp)
                if mp < self.config.min_model_agreement:
                    all_models_agree = False

            # Cap probability — no model should claim 100% certainty
            capped_prob = min(min_model_prob, self.config.max_model_probability)

            # Blend with market price for Bayesian updating
            # This acknowledges the market has information we don't (resolution source, etc.)
            blended_prob = (
                (1 - self.config.market_blend_weight) * capped_prob
                + self.config.market_blend_weight * market_price
            )

            # Apply time-based confidence scaling
            final_prob = blended_prob * time_confidence

            edge = final_prob - market_price
            agree = int(combined_prob * n_members)

            details = ", ".join(model_details)
            logger.info(
                f"    {label:20s} | combined={combined_prob:.0%} [{details}] "
                f"| final={final_prob:.0%} (cap={capped_prob:.0%},blend={blended_prob:.0%},time={time_confidence:.0%}) "
                f"| market=${market_price:.3f} | edge={edge:+.3f}"
            )

            # Skip already-traded markets
            if bracket["market_id"] in self.traded_markets:
                continue

            # Filter chain
            if edge < self.config.min_edge:
                continue
            if edge > self.config.max_edge:
                logger.info(f"      SKIP: edge {edge:.1%} exceeds cap {self.config.max_edge:.0%} — likely model error")
                continue
            if market_price > self.config.max_entry_price:
                continue
            if market_price < self.config.min_entry_price:
                continue
            if final_prob < self.config.min_model_confidence:
                continue

            # Max market divergence: skip extreme disagreements with the market
            if capped_prob - market_price > self.config.max_market_divergence:
                logger.info(f"      SKIP: model-market divergence {capped_prob - market_price:.0%} exceeds {self.config.max_market_divergence:.0%}")
                continue

            # Multi-model agreement: all models must assign meaningful probability
            if len(per_model_probs) >= 2 and not all_models_agree:
                logger.info(f"      SKIP: models disagree — not all assign ≥{self.config.min_model_agreement:.0%}")
                continue

            # Expected value calculation
            ev = final_prob * (1.0 / market_price - 1.0) * market_price - (1 - final_prob) * market_price
            # Simplified: EV per $1 bet = final_prob / market_price - 1

            signals.append(WeatherSignal(
                event_id=str(event.get("id", "")),
                market_id=bracket["market_id"],
                city=city,
                date=date,
                bracket=label,
                bracket_low=bracket["bracket_low"],
                bracket_high=bracket["bracket_high"],
                token_id=bracket["yes_token_id"],
                model_prob=final_prob,
                market_price=market_price,
                edge=edge,
                hours_remaining=hours_remaining,
                ensemble_agree=agree,
            ))

        # Only keep the SINGLE best signal per city/date.
        # Betting on multiple brackets in the same event guarantees at least
        # one loss (only one bracket wins). Pick the highest-edge signal.
        if signals:
            best = max(signals, key=lambda s: s.edge)
            logger.info(f"    -> Best bracket for {city} {date}: {best.bracket} (edge={best.edge:.1%})")
            return [best]

        return signals

    def evaluate_event_shift(self, event: dict) -> list[WeatherSignal]:
        """
        Forecast-shift detection: the core edge strategy.

        Compares current ensemble forecast probabilities against the previous
        model run's probabilities. When a bracket's probability JUMPS significantly
        (e.g., 2% -> 15%) but the market price is still cheap (<$0.05), that's
        a forecast shift the market hasn't priced in yet.

        This is how the $28K weather bot works: detect model run updates before
        the market adjusts, buy at sub-penny to $0.05 prices, hold to resolution
        for 20-500x payoffs.

        Only generates signals when:
        1. We have a previous forecast to compare against (skip first run)
        2. Probability increased by >= shift_min_prob_delta (default 10pp)
        3. New probability >= shift_min_new_prob (default 8%)
        4. Market price < shift_max_entry_price (default $0.05)
        5. Expected value multiple >= shift_min_ev_multiple (default 3x)
        """
        city_raw = event.get("_city", "")
        city = self._match_city(city_raw)
        if not city:
            return []

        date = event.get("_date", "")
        unit = event.get("_unit", "C")
        brackets = event.get("_brackets", [])
        if not brackets:
            return []

        # Check hours to resolution — shift trades work at any time
        end_date_str = event.get("endDate", "")
        try:
            end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        except Exception:
            return []

        now = datetime.now(timezone.utc)
        hours_remaining = (end_dt - now).total_seconds() / 3600.0

        if hours_remaining < 1.0:  # Don't trade within 1h of resolution
            return []
        if hours_remaining > self.config.max_hours_to_resolution:
            return []

        # Get current forecast probabilities
        combined_probs = self.weather_feed.get_bracket_probabilities(city, date, brackets, unit)
        if not combined_probs:
            return []

        signals = []
        has_previous = False

        for i, bracket in enumerate(brackets):
            label = bracket["group_item_title"]
            market_price = bracket["yes_price"]
            current_prob = combined_probs[i]
            cache_key = (city, date, label)

            prev_prob = self._prev_forecast_probs.get(cache_key)

            # Always update the cache with current probability
            self._prev_forecast_probs[cache_key] = current_prob

            if prev_prob is None:
                continue  # First run for this bracket, skip
            has_previous = True

            # Calculate probability delta
            delta = current_prob - prev_prob

            # Skip if no significant upward shift
            if delta < self.config.shift_min_prob_delta:
                continue

            # New probability must be meaningful
            if current_prob < self.config.shift_min_new_prob:
                continue

            # Market price must be cheap (the whole point — asymmetric payoff)
            if market_price > self.config.shift_max_entry_price:
                continue
            if market_price < 0.001:  # Can't buy at literally zero
                continue

            # Expected value check: prob/price must exceed threshold
            ev_multiple = current_prob / market_price
            if ev_multiple < self.config.shift_min_ev_multiple:
                continue

            # Already traded?
            if bracket["market_id"] in self.traded_markets:
                continue

            edge = current_prob - market_price

            logger.info(
                f"  [SHIFT] {city} {date} {label}: "
                f"prob {prev_prob:.0%} -> {current_prob:.0%} (delta={delta:+.0%}) | "
                f"market=${market_price:.3f} | EV={ev_multiple:.1f}x | "
                f"hours_left={hours_remaining:.0f}h"
            )

            signals.append(WeatherSignal(
                event_id=str(event.get("id", "")),
                market_id=bracket["market_id"],
                city=city,
                date=date,
                bracket=label,
                bracket_low=bracket["bracket_low"],
                bracket_high=bracket["bracket_high"],
                token_id=bracket["yes_token_id"],
                model_prob=current_prob,
                market_price=market_price,
                edge=edge,
                hours_remaining=hours_remaining,
                ensemble_agree=int(current_prob * 82),  # approximate
                source="shift",
            ))

        if not has_previous:
            logger.info(f"  [SHIFT] {city} {date}: First forecast run — caching baseline (no trades)")
        elif not signals:
            # Log top deltas for diagnostics (even when no signal fires)
            deltas = []
            for i, bracket in enumerate(brackets):
                label = bracket["group_item_title"]
                cache_key = (city, date, label)
                prev = self._prev_forecast_probs.get(cache_key)
                curr = combined_probs[i]
                if prev is not None:
                    deltas.append((label, prev, curr, curr - prev, bracket["yes_price"]))
            if deltas:
                top = max(deltas, key=lambda d: abs(d[3]))
                if abs(top[3]) > 0.001:  # Only log if there's any movement at all
                    logger.info(
                        f"  [SHIFT] {city} {date}: max delta = {top[0]} "
                        f"{top[1]:.1%}->{top[2]:.1%} ({top[3]:+.1%}) mkt=${top[4]:.3f}"
                    )

        # Best single signal per event (highest EV multiple)
        if signals:
            best = max(signals, key=lambda s: s.model_prob / max(s.market_price, 0.001))
            logger.info(
                f"    -> SHIFT SIGNAL: {best.city} {best.date} {best.bracket} | "
                f"prob={best.model_prob:.0%} vs market=${best.market_price:.3f} | "
                f"EV={best.model_prob / best.market_price:.0f}x"
            )
            return [best]

        return []

    def evaluate_event_ev(self, event: dict) -> list[WeatherSignal]:
        """
        Static EV trading: buy cheap brackets where our ensemble model
        gives meaningfully higher probability than the market price.

        No shift detection needed — just compare current model probability
        to current market price. At sub-$0.05 prices, even modest model
        accuracy yields massive EV multiples (5-50x).

        Only generates signals when:
        1. Market price < ev_max_entry_price ($0.05)
        2. Model probability >= ev_min_model_prob (5%)
        3. EV multiple (prob/price) >= ev_min_multiple (3x)
        4. Haven't already traded this bracket
        """
        city_raw = event.get("_city", "")
        city = self._match_city(city_raw)
        if not city:
            return []

        date = event.get("_date", "")
        unit = event.get("_unit", "C")
        brackets = event.get("_brackets", [])
        if not brackets:
            return []

        # Check hours to resolution
        end_date_str = event.get("endDate", "")
        try:
            end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        except Exception:
            return []

        now = datetime.now(timezone.utc)
        hours_remaining = (end_dt - now).total_seconds() / 3600.0

        if hours_remaining < self.config.min_hours_to_resolution:
            return []
        if hours_remaining > self.config.max_hours_to_resolution:
            return []

        # Get ensemble probabilities
        combined_probs = self.weather_feed.get_bracket_probabilities(city, date, brackets, unit)
        if not combined_probs:
            return []

        signals = []
        for i, bracket in enumerate(brackets):
            label = bracket["group_item_title"]
            market_price = bracket["yes_price"]
            model_prob = combined_probs[i]

            # Must be cheap (asymmetric payoff zone)
            if market_price > self.config.ev_max_entry_price:
                continue
            if market_price < 0.001:
                continue

            # Model must give meaningful probability
            if model_prob < self.config.ev_min_model_prob:
                continue

            # EV check
            ev_multiple = model_prob / market_price
            if ev_multiple < self.config.ev_min_multiple:
                continue

            # Already traded?
            if bracket["market_id"] in self.traded_markets:
                continue

            edge = model_prob - market_price

            signals.append(WeatherSignal(
                event_id=str(event.get("id", "")),
                market_id=bracket["market_id"],
                city=city,
                date=date,
                bracket=label,
                bracket_low=bracket["bracket_low"],
                bracket_high=bracket["bracket_high"],
                token_id=bracket["yes_token_id"],
                model_prob=model_prob,
                market_price=market_price,
                edge=edge,
                hours_remaining=hours_remaining,
                ensemble_agree=int(model_prob * 82),
                source="ev",
            ))

        # Best signal per event (highest EV multiple)
        if signals:
            best = max(signals, key=lambda s: s.model_prob / max(s.market_price, 0.001))
            ev = best.model_prob / max(best.market_price, 0.001)
            logger.info(
                f"  [EV] {best.city} {best.date} {best.bracket}: "
                f"prob={best.model_prob:.0%} mkt=${best.market_price:.3f} "
                f"EV={ev:.0f}x hrs={best.hours_remaining:.0f}"
            )
            return [best]

        return []

    def evaluate_event_basket(self, event: dict) -> list[WeatherSignal]:
        """
        Basket sizing: buy 3-5 adjacent brackets centered on ensemble forecast.

        Instead of binary single-bracket bet, spread capital across the high-probability
        zone. If forecast says ~11°C, buy 10°C, 11°C, 12°C. Even if off by 1°C,
        one leg wins and covers the others at 5-70x payout ratios.
        """
        city_raw = event.get("_city", "")
        city = self._match_city(city_raw)
        if not city:
            return []

        date = event.get("_date", "")
        unit = event.get("_unit", "C")
        brackets = event.get("_brackets", [])
        if not brackets:
            return []

        # Check hours to resolution
        end_date_str = event.get("endDate", "")
        try:
            end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        except Exception:
            return []

        now = datetime.now(timezone.utc)
        hours_remaining = (end_dt - now).total_seconds() / 3600.0

        if hours_remaining < self.config.min_hours_to_resolution:
            return []
        if hours_remaining > self.config.max_hours_to_resolution:
            return []

        # Skip if already traded this city+date
        event_key = f"{city}|{date}"
        if event_key in self.traded_events:
            return []

        # Get combined probability from all ensemble members
        combined_probs = self.weather_feed.get_bracket_probabilities(city, date, brackets, unit)
        if not combined_probs:
            logger.debug(f"  [BASKET] {city} {date}: No forecast data available")
            return []

        # Per-model cross-validation — check model means don't diverge too much
        model_means = self.weather_feed.get_model_means_c(city, date)
        if len(model_means) >= 2:
            mean_vals = list(model_means.values())
            divergence = max(mean_vals) - min(mean_vals)
            if divergence > self.config.max_model_divergence_c:
                logger.info(
                    f"  [BASKET] {city} {date}: SKIP — model means diverge by {divergence:.1f}°C"
                )
                return []

        forecast = self.weather_feed.get_forecast(city, date)
        n_members = len(forecast.ensemble_temps_c) if forecast else 0

        # Time-based confidence scaling (same as evaluate_event)
        if hours_remaining <= self.config.time_confidence_hours:
            time_confidence = 1.0
        else:
            ratio = (hours_remaining - self.config.time_confidence_hours) / (
                self.config.max_hours_to_resolution - self.config.time_confidence_hours
            )
            time_confidence = max(
                self.config.time_confidence_floor,
                1.0 - ratio * (1.0 - self.config.time_confidence_floor),
            )

        # Find peak bracket (highest model probability)
        peak_idx = max(range(len(combined_probs)), key=lambda i: combined_probs[i])

        # Select basket legs — expand outward from peak
        max_half = self.config.basket_max_legs // 2
        candidate_legs = []

        for i, bracket in enumerate(brackets):
            # Must be within max_half brackets of peak
            if abs(i - peak_idx) > max_half:
                continue

            model_prob = combined_probs[i]
            market_price = bracket["yes_price"]

            # Blend with market price (same as main strategy)
            capped_prob = min(model_prob, self.config.max_model_probability)
            blended_prob = (
                (1 - self.config.market_blend_weight) * capped_prob
                + self.config.market_blend_weight * market_price
            )
            final_prob = blended_prob * time_confidence

            edge = final_prob - market_price

            # Filter criteria for basket legs (looser than single-bracket)
            if model_prob < self.config.basket_min_prob:
                continue
            if edge < self.config.basket_min_leg_edge:
                continue
            if market_price > self.config.basket_max_leg_price:
                continue
            if market_price < 0.001:
                continue
            if bracket["market_id"] in self.traded_markets:
                continue

            candidate_legs.append({
                "index": i,
                "bracket": bracket,
                "model_prob": model_prob,
                "final_prob": final_prob,
                "market_price": market_price,
                "edge": edge,
            })

        # Check minimum legs — if fewer than basket_min_legs, not a basket
        if len(candidate_legs) < self.config.basket_min_legs:
            if candidate_legs:
                logger.info(
                    f"  [BASKET] {city} {date}: Only {len(candidate_legs)} legs "
                    f"(need {self.config.basket_min_legs}) — skipping"
                )
            return []

        # Limit to basket_max_legs (keep legs closest to peak by edge)
        if len(candidate_legs) > self.config.basket_max_legs:
            candidate_legs.sort(key=lambda l: l["edge"], reverse=True)
            candidate_legs = candidate_legs[:self.config.basket_max_legs]
            # Re-sort by bracket index for logging
            candidate_legs.sort(key=lambda l: l["index"])

        # Calculate per-leg sizing
        balance = self.get_balance()
        total_budget = min(self.config.basket_max_total, balance * self.config.basket_total_percent)

        total_edge = sum(l["edge"] for l in candidate_legs)
        if total_edge <= 0:
            return []

        # Weight each leg by edge (higher edge -> more capital)
        for leg in candidate_legs:
            leg["raw_size"] = total_budget * leg["edge"] / total_edge

        # Enforce minimum leg size — drop legs below minimum, re-normalize
        viable_legs = [l for l in candidate_legs if l["raw_size"] >= self.config.basket_min_leg_size]

        # Re-check min legs after dropping small ones
        if len(viable_legs) < self.config.basket_min_legs:
            # Try equal-sizing as fallback if we have enough legs
            equal_size = total_budget / len(candidate_legs)
            if equal_size >= self.config.basket_min_leg_size and len(candidate_legs) >= self.config.basket_min_legs:
                viable_legs = candidate_legs
                for leg in viable_legs:
                    leg["raw_size"] = equal_size
            else:
                logger.info(
                    f"  [BASKET] {city} {date}: Legs below min size after sizing "
                    f"({len(viable_legs)} viable, need {self.config.basket_min_legs})"
                )
                return []

        # Re-normalize sizes to fill budget
        viable_total_edge = sum(l["edge"] for l in viable_legs)
        viable_budget = min(total_budget, balance * self.config.basket_total_percent)
        for leg in viable_legs:
            leg["size"] = viable_budget * leg["edge"] / viable_total_edge
            leg["size"] = max(leg["size"], self.config.basket_min_leg_size)

        # Cap total spend
        actual_total = sum(l["size"] for l in viable_legs)
        if actual_total > total_budget:
            scale = total_budget / actual_total
            for leg in viable_legs:
                leg["size"] *= scale

        actual_total = sum(l["size"] for l in viable_legs)

        # Log basket summary
        means_str = " | ".join(f"{n}={m:.1f}°C" for n, m in model_means.items())
        logger.info(
            f"\n  [BASKET] {city} {date} ({len(viable_legs)} legs, "
            f"${actual_total:.2f} total, {hours_remaining:.1f}h left) [{means_str}]"
        )

        # Build signals
        signals = []
        ev_per_outcome = {}

        for leg in viable_legs:
            bracket = leg["bracket"]
            label = bracket["group_item_title"]
            shares = leg["size"] / leg["market_price"]
            payout_if_win = shares  # $1 per share
            profit_if_win = payout_if_win - actual_total  # profit = payout - total basket cost

            peak_marker = " <-- peak" if leg["index"] == peak_idx else ""
            logger.info(
                f"    {label:20s}: prob={leg['model_prob']:.0%} mkt=${leg['market_price']:.3f} "
                f"edge={leg['edge']:+.0%} -> ${leg['size']:.2f} ({shares:.0f} shares){peak_marker}"
            )

            ev_per_outcome[label] = profit_if_win

            signals.append(WeatherSignal(
                event_id=str(event.get("id", "")),
                market_id=bracket["market_id"],
                city=city,
                date=date,
                bracket=label,
                bracket_low=bracket["bracket_low"],
                bracket_high=bracket["bracket_high"],
                token_id=bracket["yes_token_id"],
                model_prob=leg["final_prob"],
                market_price=leg["market_price"],
                edge=leg["edge"],
                hours_remaining=hours_remaining,
                ensemble_agree=int(leg["model_prob"] * n_members),
                source="basket",
                basket_size=leg["size"],
            ))

        # Basket EV summary
        basket_ev = sum(
            leg["final_prob"] * ev_per_outcome[leg["bracket"]["group_item_title"]]
            for leg in viable_legs
        )
        # Add the "miss" scenario: none of our legs win
        miss_prob = 1.0 - sum(leg["final_prob"] for leg in viable_legs)
        miss_pnl = -actual_total
        basket_ev += miss_prob * miss_pnl

        outcome_strs = [
            f"if {leg['bracket']['group_item_title']}: ${ev_per_outcome[leg['bracket']['group_item_title']]:+.2f}"
            for leg in viable_legs
        ]
        logger.info(
            f"    Basket EV: ${basket_ev:+.2f} | "
            f"{', '.join(outcome_strs)}, miss({miss_prob:.0%}): -${actual_total:.2f}"
        )

        return signals

    async def execute_trade(self, signal: WeatherSignal) -> Optional[WeatherTrade]:
        """
        Execute a weather trade with Kelly-based position sizing.

        Kelly formula: fraction = edge / (1 - entry_price)
        We use quarter-Kelly for safety, then clamp to min/max trade size.
        """
        balance = self.get_balance()
        is_obs = signal.source in ("observation", "near_noon_obs")
        is_ev = signal.source == "ev"
        is_basket = signal.source == "basket"

        # Kelly-based sizing: bigger edge → bigger trade
        if signal.market_price >= 1.0:
            return None

        if signal.basket_size is not None:
            # Basket leg — size pre-calculated by evaluate_event_basket()
            trade_size = signal.basket_size
        elif is_ev:
            # EV trades use fixed sizing for asymmetric payoffs.
            # At $0.01-0.05 entry, $5 buys 100-500 shares.
            # If it hits: $100-$500 payout. If not: -$5.
            trade_size = min(self.config.ev_trade_size, balance * 0.10)
        else:
            kelly_raw = signal.edge / (1.0 - signal.market_price)
            kelly_size = self.config.kelly_fraction * kelly_raw * balance

            # Use observation-specific sizing for higher-confidence observation trades
            max_size = self.config.obs_max_trade_size if is_obs else self.config.max_trade_size
            pct = self.config.obs_trade_percent if is_obs else self.config.trade_percent

            # Clamp to configured bounds — let Kelly actually differentiate
            trade_size = kelly_size
            trade_size = min(trade_size, max_size)
            trade_size = min(trade_size, balance * pct)

        trade_size = max(trade_size, self.config.min_trade_size)  # enforce floor

        if trade_size > balance:
            logger.warning(f"Insufficient balance: ${balance:.2f} < trade ${trade_size:.2f}")
            return None

        if self.position_count >= self.config.max_concurrent_positions:
            logger.info(f"At max positions ({self.position_count}/{self.config.max_concurrent_positions})")
            return None

        city_positions = self.city_position_count.get(signal.city, 0)
        if city_positions >= self.config.max_positions_per_city:
            logger.info(f"At max positions for {signal.city} ({city_positions}/{self.config.max_positions_per_city})")
            return None

        # Re-fetch best ask for freshest price
        if is_basket:
            max_entry = self.config.basket_max_leg_price
            min_edge = self.config.basket_min_leg_edge
        elif is_ev:
            max_entry = self.config.ev_max_entry_price
            min_edge = 0.0  # EV trades rely on EV multiple, not raw edge
        elif is_obs:
            max_entry = self.config.obs_max_entry_price
            min_edge = self.config.obs_min_edge
        else:
            max_entry = self.config.max_entry_price
            min_edge = self.config.min_edge
        best_ask = self.polymarket.get_best_ask(signal.token_id)
        order_price = best_ask if best_ask and best_ask <= max_entry else signal.market_price

        # Recheck edge with fresh price
        fresh_edge = signal.model_prob - order_price
        if fresh_edge < min_edge:
            logger.info(f"  Edge disappeared after price refresh: {fresh_edge:.3f} < {min_edge}")
            return None

        shares = trade_size / order_price
        expected_payout = shares
        expected_profit = expected_payout - trade_size

        trade = WeatherTrade(
            timestamp=datetime.now(timezone.utc).isoformat(),
            event_id=signal.event_id,
            market_id=signal.market_id,
            city=signal.city,
            date=signal.date,
            bracket=signal.bracket,
            amount=trade_size,
            entry_price=order_price,
            expected_payout=expected_payout,
            expected_profit=expected_profit,
            model_prob=signal.model_prob,
            market_price_at_entry=order_price,
            edge_at_entry=fresh_edge,
            ensemble_agree=signal.ensemble_agree,
            hours_remaining=signal.hours_remaining,
            status="PENDING",
        )

        # Expected value: prob * profit_if_win + (1-prob) * (-cost)
        ev = signal.model_prob * expected_profit + (1 - signal.model_prob) * (-trade_size)

        logger.info(f"\n{'='*60}")
        logger.info("WEATHER TRADE SIGNAL")
        logger.info(f"City: {signal.city} | Date: {signal.date}")
        logger.info(f"Bracket: {signal.bracket} @ ${order_price:.3f}")
        logger.info(f"Model prob: {signal.model_prob:.1%} | Edge: {fresh_edge:.1%}")
        logger.info(f"Time remaining: {signal.hours_remaining:.1f}h")
        logger.info(f"Trade: ${trade_size:.2f} -> {shares:.1f} shares")
        logger.info(f"If WIN: +${expected_profit:.2f} | If LOSS: -${trade_size:.2f}")
        logger.info(f"Expected value: ${ev:+.2f} (EV/cost: {ev/trade_size:+.0%})")
        logger.info(f"{'='*60}\n")

        if self.dry_run:
            logger.info("[DRY RUN] Trade simulated (not placed on chain)")
            trade.status = "DRY_RUN"
            trade.end_date = signal.date
            self.successful_trades += 1
            self.traded_markets.add(signal.market_id)
            if not is_basket:
                # Basket signals: traded_events updated in run loop after all legs execute
                self.traded_events.add(f"{signal.city}|{signal.date}")
            self.city_position_count[signal.city] = city_positions + 1
            self.position_count += 1
            self.pending_resolutions.append(trade)
        else:
            try:
                logger.warning(">>> PLACING LIVE LIMIT ORDER <<<")
                order_response = self.polymarket.execute_limit_buy(
                    token_id=signal.token_id,
                    price=order_price,
                    size=round(shares, 2),
                )

                order_id = None
                if isinstance(order_response, dict):
                    order_id = order_response.get("orderID") or order_response.get("id")
                elif isinstance(order_response, str):
                    order_id = order_response

                trade.order_id = str(order_id) if order_id else None
                logger.info(f"Order placed: {order_id}")

                filled = await self._wait_for_fill(order_id, signal.token_id)

                if filled:
                    trade.status = "FILLED"
                    trade.end_date = signal.date
                    self.successful_trades += 1
                    self.position_count += 1
                    self.city_position_count[signal.city] = city_positions + 1
                    self.traded_markets.add(signal.market_id)
                    if not is_basket:
                        # Basket signals: traded_events updated in run loop after all legs execute
                        self.traded_events.add(f"{signal.city}|{signal.date}")
                    self.pending_resolutions.append(trade)
                    logger.info("Order FILLED! Queued for resolution verification.")
                else:
                    if order_id:
                        try:
                            self.polymarket.cancel_order(str(order_id))
                            logger.info(f"Order cancelled after {self.config.order_timeout}s timeout")
                        except Exception as e:
                            logger.warning(f"Failed to cancel order: {e}")
                    trade.status = "TIMEOUT_CANCELLED"
                    self.failed_trades += 1

            except Exception as e:
                logger.error(f"Trade execution failed: {e}")
                trade.status = "FAILED"
                self.failed_trades += 1
                self.risk_manager.record_failed_market(signal.market_id)

        self.total_trades += 1
        self.trade_history.append(trade)
        self._log_trade(trade)

        # Telegram alert
        if trade.status in ("FILLED", "DRY_RUN"):
            try:
                if is_basket:
                    source_label = "BASKET"
                    obs_detail = f"${trade_size:.2f} leg"
                elif is_ev:
                    source_label = "STATIC EV"
                    ev_mult = signal.model_prob / max(order_price, 0.001)
                    obs_detail = f"EV={ev_mult:.0f}x"
                elif is_obs:
                    source_label = "NEAR-NOON obs" if signal.source == "near_noon_obs" else "METAR obs"
                    obs_detail = f"{signal.ensemble_agree} obs"
                else:
                    source_label = "Ensemble"
                    obs_detail = f"{signal.ensemble_agree} members agree"
                self.telegram.send_message_sync(
                    f"{'[DRY] ' if self.dry_run else ''}🌡️ <b>WEATHER {trade.status}</b> [{source_label}]\n\n"
                    f"🏙️ {signal.city} — {signal.date}\n"
                    f"🌤️ Bracket: {signal.bracket} @ ${order_price:.3f}\n"
                    f"🔬 {source_label}: {signal.model_prob:.0%} ({obs_detail})\n"
                    f"📈 Edge: {fresh_edge:.0%}\n"
                    f"💵 Cost: ${trade_size:.2f} | Shares: {shares:.1f}\n"
                    f"🎯 Payout if win: ${expected_payout:.2f} (+${expected_profit:.2f})\n"
                )
            except Exception:
                pass

        return trade

    async def _wait_for_fill(self, order_id: Optional[str], token_id: str) -> bool:
        """Wait for a limit order to fill, up to order_timeout seconds."""
        if not order_id:
            return False

        start = time.time()
        while (time.time() - start) < self.config.order_timeout:
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
            await asyncio.sleep(2.0)

        return False

    def _log_trade(self, trade: WeatherTrade):
        """Log trade to file."""
        try:
            with open('weather_trades.log', 'a') as f:
                f.write(json.dumps(asdict(trade)) + '\n')
        except Exception as e:
            logger.error(f"Failed to log trade: {e}")

    async def check_resolutions(self):
        """
        Check if any pending trades have resolved.

        Weather markets resolve at noon UTC on the target date.
        Queries Gamma API for each pending trade's event to check resolution.
        """
        if not self.pending_resolutions:
            return

        now = datetime.now(timezone.utc)
        still_pending = []

        for trade in self.pending_resolutions:
            # Parse the target date — resolution is at noon UTC
            try:
                target = datetime.strptime(trade.date, "%Y-%m-%d").replace(
                    hour=12, tzinfo=timezone.utc
                )
            except Exception:
                still_pending.append(trade)
                continue

            # Wait until 1 hour past resolution time for Polymarket to mark it
            if now < target + timedelta(hours=1):
                still_pending.append(trade)
                continue

            # Query the event to check which bracket won
            resolved_bracket = await self._get_resolved_bracket(trade)

            if resolved_bracket is None:
                # Keep checking for up to 6 hours past resolution
                if now < target + timedelta(hours=6):
                    still_pending.append(trade)
                else:
                    logger.warning(f"Could not verify resolution for {trade.city} {trade.date} after 6h")
                    trade.resolution_status = "unknown"
                continue

            trade.resolved_bracket = resolved_bracket
            won = (trade.bracket == resolved_bracket)

            if won:
                trade.resolution_status = "win"
                trade.actual_profit = trade.expected_profit
                self.verified_wins += 1
                self.verified_pnl += trade.actual_profit
                if self.simulated_balance is not None:
                    self.simulated_pnl += trade.actual_profit
                logger.info(
                    f"VERIFIED WIN: {trade.city} {trade.date} | "
                    f"Bet {trade.bracket}, resolved {resolved_bracket} | "
                    f"Profit: +${trade.actual_profit:.2f}"
                )
            else:
                trade.resolution_status = "loss"
                trade.actual_profit = -trade.amount
                self.verified_losses += 1
                self.verified_pnl += trade.actual_profit
                if self.simulated_balance is not None:
                    self.simulated_pnl += trade.actual_profit
                logger.info(
                    f"VERIFIED LOSS: {trade.city} {trade.date} | "
                    f"Bet {trade.bracket}, resolved {resolved_bracket} | "
                    f"Loss: -${trade.amount:.2f}"
                )

            self._log_trade(trade)

            # Telegram notification
            total_resolved = self.verified_wins + self.verified_losses
            win_rate = self.verified_wins / total_resolved * 100 if total_resolved > 0 else 0
            try:
                result_emoji = "☀️" if won else "🌧️"
                self.telegram.send_message_sync(
                    f"{'[DRY] ' if self.dry_run else ''}{result_emoji} <b>WEATHER {'WIN' if won else 'LOSS'}</b>\n\n"
                    f"🏙️ {trade.city} — {trade.date}\n"
                    f"🌡️ Bet: {trade.bracket} | Actual: {resolved_bracket}\n"
                    f"💰 P&L: ${trade.actual_profit:+.2f}\n\n"
                    f"📊 Record: {self.verified_wins}W/{self.verified_losses}L ({win_rate:.0f}%)\n"
                    f"💵 Session P&L: ${self.verified_pnl:+.2f}\n"
                )
            except Exception:
                pass

            # Free up position count
            self.position_count = max(0, self.position_count - 1)
            city_count = self.city_position_count.get(trade.city, 1)
            self.city_position_count[trade.city] = max(0, city_count - 1)

        self.pending_resolutions = still_pending

    async def _get_resolved_bracket(self, trade: WeatherTrade) -> Optional[str]:
        """Query Gamma API to determine which bracket won."""
        try:
            response = httpx.get(
                f"https://gamma-api.polymarket.com/events/{trade.event_id}",
                timeout=10,
            )
            if response.status_code != 200:
                return None

            event = response.json()
            markets = event.get("markets", [])

            for mkt in markets:
                prices = mkt.get("outcomePrices", "[]")
                if isinstance(prices, str):
                    prices = json.loads(prices)
                if len(prices) < 2:
                    continue

                yes_price = float(prices[0])
                # Winner has YES price >= 0.95
                if yes_price >= 0.95:
                    return mkt.get("groupItemTitle", "")

            return None

        except Exception as e:
            logger.debug(f"Error checking resolution for {trade.city} {trade.date}: {e}")
            return None

    def print_status(self):
        """Print current status scoreboard."""
        balance = self.get_balance()

        logger.info(f"\n{'='*50}")
        logger.info("WEATHER TRADER STATUS")
        logger.info(f"{'='*50}")
        logger.info(f"Balance: ${balance:.2f}")
        if self.initial_balance:
            pnl = balance - self.initial_balance
            logger.info(f"Session P&L: ${pnl:+.2f}")
        logger.info(f"Trades: {self.total_trades} (fills: {self.successful_trades}, failed: {self.failed_trades})")
        logger.info(f"Open positions: {self.position_count}/{self.config.max_concurrent_positions}")

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

    async def run(self, scan_interval: Optional[float] = None, max_iterations: Optional[int] = None):
        """
        Main async event loop — V7 basket + observation weather trading:

        1. BASKET MODE (primary — all 14 cities, every scan):
           Buy 3-5 adjacent brackets centered on ensemble forecast peak.
           Hedges the 1-2°C forecast error that killed every single-bin approach.

        2. OBSERVATION MODE (supplement — Seoul/Wellington only):
           Near resolution, use METAR airport observations to confirm
           which bracket will win. Buy if market hasn't adjusted.

        3. STATIC EV (disabled by default — 0/15 track record):
           Buy cheap brackets with high model probability.
        """
        basket_mode = self.config.basket_mode
        ev_mode = self.config.ev_mode
        obs_mode = self.config.observation_mode

        # Scan interval: use basket interval (30 min) when basket-only,
        # obs interval (10 min) if obs is also active
        if scan_interval:
            interval = scan_interval
        elif obs_mode:
            interval = self.config.obs_scan_interval  # 10 min
        elif basket_mode:
            interval = self.config.basket_scan_interval  # 30 min
        else:
            interval = self.config.obs_scan_interval  # fallback 10 min

        forecast_interval = self.config.basket_forecast_refresh  # 30 min

        high_conf = ", ".join(sorted(OBSERVATION_HIGH_CONFIDENCE))
        near_noon = ", ".join(sorted(OBSERVATION_NEAR_NOON))
        logger.info(f"\n{'='*60}")
        logger.info("STARTING WEATHER TRADER V7 — BASKET + OBSERVATION")
        logger.info(f"{'='*60}")
        if basket_mode:
            logger.info(f"Basket: ON (all 14 cities)")
            logger.info(f"  Max legs: {self.config.basket_max_legs} | Min legs: {self.config.basket_min_legs}")
            logger.info(f"  Budget: {self.config.basket_total_percent:.0%} of balance, max ${self.config.basket_max_total:.0f}")
            logger.info(f"  Min leg size: ${self.config.basket_min_leg_size:.0f} | Min leg edge: {self.config.basket_min_leg_edge:.0%}")
            logger.info(f"  Max leg price: ${self.config.basket_max_leg_price:.2f} | Min prob: {self.config.basket_min_prob:.0%}")
            logger.info(f"  Forecast refresh: every {forecast_interval/60:.0f} min")
        else:
            logger.info("Basket: OFF")
        if ev_mode:
            logger.info(f"Static EV: ON (all 14 cities)")
            logger.info(f"  Max entry: ${self.config.ev_max_entry_price:.3f}")
            logger.info(f"  Min EV multiple: {self.config.ev_min_multiple:.0f}x")
        else:
            logger.info("Static EV: OFF")
        if obs_mode:
            logger.info(f"Observation HIGH CONF: {high_conf}")
            logger.info(f"  Buffer: {self.config.obs_boundary_buffer_c:.1f}°C | Max hours: {self.config.obs_max_hours_to_resolution:.0f}h")
            if near_noon:
                logger.info(f"Observation NEAR-NOON: {near_noon}")
                logger.info(f"  Buffer: {self.config.near_noon_buffer_c:.1f}°C | Max hours: {self.config.near_noon_max_hours:.0f}h | Conf cap: {self.config.near_noon_max_confidence:.0%}")
        else:
            logger.info("Observation: OFF")
        logger.info(f"Scan interval: {interval}s ({interval/60:.0f} min)")
        logger.info(f"Mode: {'DRY RUN' if self.dry_run else 'LIVE TRADING'}")
        logger.info(f"{'='*60}\n")

        if not self.dry_run:
            logger.warning("=" * 60)
            logger.warning("WARNING: LIVE TRADING MODE - REAL FUNDS AT RISK")
            logger.warning("=" * 60)
            await asyncio.sleep(5)

        # Initialize balance
        balance = self.get_balance()
        self.initial_balance = balance
        logger.info(f"Starting balance: ${balance:.2f}")

        if balance < self.config.min_trade_size:
            logger.error(f"Balance ${balance:.2f} below min trade size ${self.config.min_trade_size}")
            return

        # Initial data fetch
        if ev_mode or basket_mode:
            logger.info("Fetching initial ensemble forecasts...")
            fc_count = await self.weather_feed.update()
            logger.info(f"Loaded {fc_count} city-date forecasts")
            self._last_forecast_fetch = time.time()

        if obs_mode:
            logger.info("Fetching initial METAR observations...")
            obs_count = await self.obs_feed.update()
            logger.info(f"Loaded {obs_count} METAR observation entries")

        # Telegram startup
        try:
            modes = []
            if basket_mode:
                modes.append(f"Basket ({self.config.basket_min_legs}-{self.config.basket_max_legs} legs, max ${self.config.basket_max_total:.0f})")
            if ev_mode:
                modes.append(f"Static EV (max ${self.config.ev_max_entry_price}, {self.config.ev_min_multiple:.0f}x+)")
            if obs_mode:
                modes.append(f"Obs: {high_conf}")
                if near_noon:
                    modes.append(f"Near-noon: {near_noon}")
            self.telegram.send_message_sync(
                f"{'[DRY] ' if self.dry_run else ''}<b>WEATHER TRADER V7 STARTED</b>\n\n"
                f"Strategy: {' | '.join(modes)}\n"
                f"Balance: ${balance:.2f}\n"
                f"Scan: {interval/60:.0f}min\n"
            )
        except Exception:
            pass

        iteration = 0

        try:
            while max_iterations is None or iteration < max_iterations:
                iteration += 1
                logger.info(f"\n--- Scan {iteration} ---")

                # Refresh ensemble forecasts periodically
                now_ts = time.time()
                forecast_due = (ev_mode or basket_mode) and (
                    now_ts - self._last_forecast_fetch >= forecast_interval
                )

                if forecast_due or ((ev_mode or basket_mode) and iteration == 1):
                    logger.info("Refreshing ensemble forecasts...")
                    await self.weather_feed.update()
                    self._last_forecast_fetch = now_ts

                # Update METAR observations (every scan, lightweight)
                if obs_mode and iteration > 1:
                    await self.obs_feed.update()

                # Fetch active temperature markets
                logger.info("Fetching active weather markets from Polymarket...")
                try:
                    events = self.gamma.get_weather_markets()
                except Exception as e:
                    logger.error(f"Failed to fetch weather markets: {e}")
                    events = []

                logger.info(f"Found {len(events)} active temperature markets")

                # ---- Evaluate all strategies ----
                all_signals = []
                basket_event_keys: set[str] = set()

                # 1. Basket mode (primary — all cities, every scan)
                if basket_mode:
                    basket_count = 0
                    for event in events:
                        basket_signals = self.evaluate_event_basket(event)
                        if basket_signals:
                            key = f"{basket_signals[0].city}|{basket_signals[0].date}"
                            basket_event_keys.add(key)
                            basket_count += len(basket_signals)
                        all_signals.extend(basket_signals)
                    if basket_count:
                        logger.info(f"  Basket signals: {basket_count} legs across {len(basket_event_keys)} events")

                # 2. Static EV (disabled by default)
                if ev_mode:
                    ev_count = 0
                    for event in events:
                        ev_signals = self.evaluate_event_ev(event)
                        all_signals.extend(ev_signals)
                        ev_count += len(ev_signals)
                    if ev_count:
                        logger.info(f"  Static EV signals: {ev_count}")

                # 3. Observation mode (supplement — near-resolution, high-conf cities)
                if obs_mode:
                    for event in events:
                        obs_signals = self.evaluate_event_observation(event)
                        all_signals.extend(obs_signals)

                # ---- Execute trades ----
                if all_signals:
                    # Sort by EV multiple (prob/price), highest first
                    all_signals.sort(
                        key=lambda s: s.model_prob / max(s.market_price, 0.001),
                        reverse=True,
                    )
                    logger.info(f"\nFound {len(all_signals)} trade signals:")
                    for sig in all_signals[:15]:
                        tag = sig.source.upper()
                        ev = sig.model_prob / max(sig.market_price, 0.001)
                        size_str = f" ${sig.basket_size:.2f}" if sig.basket_size else ""
                        logger.info(
                            f"  [{tag}] {sig.city} {sig.date} {sig.bracket} | "
                            f"prob={sig.model_prob:.0%} | market=${sig.market_price:.3f} | "
                            f"EV={ev:.0f}x{size_str}"
                        )

                    executed = 0
                    for sig in all_signals:
                        if self.position_count >= self.config.max_concurrent_positions:
                            break
                        result = await self.execute_trade(sig)
                        if result and result.status in ("FILLED", "DRY_RUN"):
                            executed += 1
                    logger.info(f"Executed {executed} trades this scan")

                    # Add basket event keys to traded_events AFTER all legs execute
                    for key in basket_event_keys:
                        self.traded_events.add(key)
                else:
                    logger.info("No trade signals this scan")

                # Check resolutions
                await self.check_resolutions()

                self.print_status()

                # Wait for next scan
                if max_iterations is None or iteration < max_iterations:
                    logger.info(f"Next scan in {interval/60:.0f} min...")
                    await asyncio.sleep(interval)

        except KeyboardInterrupt:
            logger.info("\nBot stopped by user")
        except Exception as e:
            logger.error(f"Error in main loop: {e}", exc_info=True)
        finally:
            self.print_status()
            try:
                self.telegram.send_message_sync(
                    f"<b>WEATHER TRADER STOPPED</b>\n\n"
                    f"Trades: {self.total_trades}\n"
                    f"Fills: {self.successful_trades}\n"
                    f"Record: {self.verified_wins}W/{self.verified_losses}L\n"
                    f"P&L: ${self.verified_pnl:+.2f}\n"
                    f"Balance: ${self.get_balance():.2f}\n"
                )
            except Exception:
                pass
            logger.info("Weather trader stopped")


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description='Polymarket Weather Temperature Trader V7 (Basket + Observation)')
    parser.add_argument('--dry-run', action='store_true', default=True,
                        help='Run in dry-run mode (no real trades)')
    parser.add_argument('--live', action='store_true',
                        help='Run in live mode (REAL TRADES)')
    parser.add_argument('--scan-interval', type=float, default=None,
                        help='Seconds between scans (default: 1800 basket, 600 obs)')
    parser.add_argument('--max-iterations', type=int, default=None,
                        help='Maximum scan iterations')
    parser.add_argument('--simulated-balance', type=float, default=None,
                        help='Simulated balance for dry-run testing')
    parser.add_argument('--models', type=str, default='ecmwf_ifs025,gfs_seamless',
                        help='Ensemble models, comma-separated')
    # Basket mode flags
    parser.add_argument('--no-basket', action='store_true',
                        help='Disable basket mode')
    parser.add_argument('--basket-max-total', type=float, default=None,
                        help='Max total basket spend (default: 30.0)')
    parser.add_argument('--basket-max-legs', type=int, default=None,
                        help='Max legs per basket (default: 5)')
    # Observation mode flags
    parser.add_argument('--no-observation', action='store_true',
                        help='Disable observation mode')
    # EV mode flags
    parser.add_argument('--no-ev', action='store_true',
                        help='Disable static EV trading')
    parser.add_argument('--ev-max-price', type=float, default=None,
                        help='Max entry price for EV trades (default: 0.05)')
    parser.add_argument('--ev-min-prob', type=float, default=None,
                        help='Min model probability for EV trades (default: 0.05)')
    parser.add_argument('--ev-min-multiple', type=float, default=None,
                        help='Min EV multiple (prob/price) to trade (default: 3.0)')
    parser.add_argument('--ev-trade-size', type=float, default=None,
                        help='Fixed trade size for EV trades (default: 5.0)')
    parser.add_argument('--ev-refresh', type=float, default=None,
                        help='Forecast refresh interval in seconds (default: 1800)')

    args = parser.parse_args()

    # Add stdout logging for interactive use (won't block if piped since
    # the main log goes to FileHandler regardless)
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logging.getLogger().addHandler(console)

    config_kwargs = {}
    if args.models:
        config_kwargs['ensemble_models'] = [m.strip() for m in args.models.split(',')]
    # Basket config
    if args.no_basket:
        config_kwargs['basket_mode'] = False
    if args.basket_max_total is not None:
        config_kwargs['basket_max_total'] = args.basket_max_total
    if args.basket_max_legs is not None:
        config_kwargs['basket_max_legs'] = args.basket_max_legs
    # Observation config
    if args.no_observation:
        config_kwargs['observation_mode'] = False
    # EV config
    if args.no_ev:
        config_kwargs['ev_mode'] = False
    if args.ev_max_price is not None:
        config_kwargs['ev_max_entry_price'] = args.ev_max_price
    if args.ev_min_prob is not None:
        config_kwargs['ev_min_model_prob'] = args.ev_min_prob
    if args.ev_min_multiple is not None:
        config_kwargs['ev_min_multiple'] = args.ev_min_multiple
    if args.ev_trade_size is not None:
        config_kwargs['ev_trade_size'] = args.ev_trade_size
    if args.ev_refresh is not None:
        config_kwargs['ev_refresh_interval'] = args.ev_refresh

    config = WeatherConfig(**config_kwargs)
    risk_config = RiskConfig()
    dry_run = not args.live

    bot = WeatherTrader(
        config=config,
        risk_config=risk_config,
        dry_run=dry_run,
        simulated_balance=args.simulated_balance,
    )

    asyncio.run(bot.run(
        scan_interval=args.scan_interval,
        max_iterations=args.max_iterations,
    ))


if __name__ == "__main__":
    main()
