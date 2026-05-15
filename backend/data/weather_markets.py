"""Weather temperature market fetcher from Polymarket."""
import httpx
import re
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import List, Optional

logger = logging.getLogger("trading_bot")

# Map city names/variants found in market titles to our city keys
CITY_ALIASES = {
    "new york city": "nyc",
    "new york": "nyc",
    "nyc": "nyc",
    "chicago": "chicago",
    "miami": "miami",
    "los angeles": "los_angeles",
    "la": "los_angeles",
    "denver": "denver",
    "boston": "boston",
    "phoenix": "phoenix",
    "austin": "austin",
    "atlanta": "atlanta",
    "seattle": "seattle",
    "houston": "houston",
    "philadelphia": "philadelphia",
    "philly": "philadelphia",
    "dallas": "dallas",
    "dfw": "dallas",
    "beijing": "beijing",
    "shanghai": "shanghai",
    "chongqing": "chongqing",
    "guangzhou": "guangzhou",
    "chengdu": "chengdu",
    "wuhan": "wuhan",
    "hong kong": "hong_kong",
    "hongkong": "hong_kong",
}

# Month name to number
MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


@dataclass
class WeatherMarket:
    """A weather temperature prediction market."""
    slug: str
    market_id: str
    platform: str
    title: str
    city_key: str
    city_name: str
    target_date: date
    threshold_f: float       # Threshold in Fahrenheit (always F for downstream math)
    metric: str              # "high" or "low"
    direction: str           # "above" or "below" (binary) or "equal"/"at_or_below" (bucketed)
    yes_price: float         # Price of YES outcome (0-1)
    no_price: float          # Price of NO outcome (0-1)
    volume: float = 0.0
    closed: bool = False
    unit: str = "F"          # "F" or "C" — original quoted unit (for display)
    bucket_type: str = "binary"  # "binary" | "equality" | "floor"
    bucket_center_c: Optional[float] = None  # For equality buckets (degree center in C)
    event_id: Optional[str] = None  # For grouping buckets to pick best within event


def _parse_weather_market_title(title: str) -> Optional[dict]:
    """
    Parse a weather market title to extract city, threshold, metric, date.

    Handles patterns like:
    - "Will the high temperature in New York exceed 75°F on March 5?"
    - "NYC high temperature above 80°F on March 10, 2026"
    - "Chicago daily high over 60°F on March 3"
    - "Will Miami's low be above 65°F on March 7?"
    - "Temperature in Denver above 70°F on March 5, 2026"
    """
    title_lower = title.lower()

    # Must be temperature-related
    if not any(kw in title_lower for kw in ["temperature", "temp", "°f", "degrees", "high", "low"]):
        return None

    # Extract city
    city_key = None
    city_name = None
    for alias, key in sorted(CITY_ALIASES.items(), key=lambda x: -len(x[0])):
        if alias in title_lower:
            city_key = key
            from backend.data.weather import CITY_CONFIG
            city_name = CITY_CONFIG[key]["name"]
            break

    if not city_key:
        return None

    # Extract threshold temperature
    temp_match = re.search(r'(\d+)\s*°?\s*f', title_lower)
    if not temp_match:
        temp_match = re.search(r'(\d+)\s*degrees', title_lower)
    if not temp_match:
        return None
    threshold_f = float(temp_match.group(1))

    # Determine metric (high vs low)
    metric = "high"  # default
    if "low" in title_lower:
        metric = "low"

    # Determine direction
    direction = "above"  # default
    if any(kw in title_lower for kw in ["below", "under", "less than", "drop below"]):
        direction = "below"

    # Extract date
    target_date = _extract_date(title_lower)
    if not target_date:
        return None

    return {
        "city_key": city_key,
        "city_name": city_name,
        "threshold_f": threshold_f,
        "metric": metric,
        "direction": direction,
        "target_date": target_date,
    }


def _extract_date(text: str) -> Optional[date]:
    """Extract a date from market title text."""
    today = date.today()

    # Build month name pattern for precise matching
    month_names = "|".join(MONTH_MAP.keys())

    # Pattern: "March 5, 2026" or "March 5 2026" or "March 5"
    for match in re.finditer(rf'({month_names})\s+(\d{{1,2}})(?:\s*,?\s*(\d{{4}}))?', text):
        month_str = match.group(1)
        day = int(match.group(2))
        year = int(match.group(3)) if match.group(3) else today.year

        month = MONTH_MAP.get(month_str)
        if month and 1 <= day <= 31:
            try:
                return date(year, month, day)
            except ValueError:
                continue

    # Pattern: "3/5/2026" or "03/05"
    match = re.search(r'(\d{1,2})/(\d{1,2})(?:/(\d{4}))?', text)
    if match:
        month = int(match.group(1))
        day = int(match.group(2))
        year = int(match.group(3)) if match.group(3) else today.year
        try:
            return date(year, month, day)
        except ValueError:
            pass

    return None


async def fetch_polymarket_weather_markets(city_keys: Optional[List[str]] = None) -> List[WeatherMarket]:
    """
    Search Polymarket for weather temperature markets.

    Two paths:
      1. Public search for the global bucketed Celsius series
         ("Highest temperature in CITY on DATE?" with 11 °C buckets each).
      2. Tag=Weather fallback for legacy °F binary above/below markets.
    """
    markets: List[WeatherMarket] = []
    seen_ids = set()

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Path 1: bucketed Celsius series via public search
            try:
                response = await client.get(
                    "https://gamma-api.polymarket.com/public-search",
                    params={"q": "highest temperature", "limit_per_type": 200},
                )
                response.raise_for_status()
                data = response.json()
                events = data.get("events", []) if isinstance(data, dict) else []
                for event in events:
                    event_slug = event.get("slug", "") or ""
                    event_id = str(event.get("id", "")) if event.get("id") else None
                    for market_data in event.get("markets", []):
                        market = _parse_polymarket_bucketed(market_data, event_slug, event_id, city_keys)
                        if market and market.market_id not in seen_ids:
                            markets.append(market)
                            seen_ids.add(market.market_id)
            except Exception as e:
                logger.debug(f"Bucketed temperature search failed: {e}")

            # Path 2: legacy binary °F markets under the Weather tag
            try:
                response = await client.get(
                    "https://gamma-api.polymarket.com/events",
                    params={"closed": "false", "limit": 100, "tag": "Weather"},
                )
                response.raise_for_status()
                events = response.json()
                for event in events:
                    event_slug = event.get("slug", "") or ""
                    event_id = str(event.get("id", "")) if event.get("id") else None
                    for market_data in event.get("markets", []):
                        # Try bucketed first (some Celsius markets get the Weather tag too)
                        market = _parse_polymarket_bucketed(market_data, event_slug, event_id, city_keys)
                        if market is None:
                            market = _parse_polymarket_weather(market_data, event_slug, city_keys)
                        if market and market.market_id not in seen_ids:
                            markets.append(market)
                            seen_ids.add(market.market_id)
            except Exception as e:
                logger.debug(f"Tag=Weather search failed: {e}")

    except Exception as e:
        logger.warning(f"Failed to fetch weather markets: {e}")

    logger.info(f"Found {len(markets)} weather temperature markets")
    return markets


_BUCKETED_TITLE_RE = re.compile(
    r"^will the highest temperature in ([a-z ]+?) be\s+"
    r"(\d{1,3})\s*°?\s*c(?:\s+or below)?\s+on\s+",
    re.IGNORECASE,
)


def _celsius_to_fahrenheit(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def _parse_polymarket_bucketed(
    market_data: dict,
    event_slug: str,
    event_id: Optional[str],
    city_keys: Optional[List[str]] = None,
) -> Optional[WeatherMarket]:
    """
    Parse a Polymarket bucketed Celsius market.

    Question shapes:
      - "Will the highest temperature in Beijing be 28°C on May 16?"   (equality)
      - "Will the highest temperature in Beijing be 25°C or below on May 16?" (floor)
    """
    question = market_data.get("question") or market_data.get("groupItemTitle") or ""
    if not question:
        return None

    m = _BUCKETED_TITLE_RE.match(question.strip())
    if not m:
        return None

    city_phrase = m.group(1).strip().lower()
    degrees_c = float(m.group(2))
    is_floor = "or below" in question.lower()

    city_key = CITY_ALIASES.get(city_phrase)
    if not city_key:
        return None
    if city_keys and city_key not in city_keys:
        return None

    from backend.data.weather import CITY_CONFIG
    city_name = CITY_CONFIG.get(city_key, {}).get("name", city_phrase.title())

    target_date = _extract_date(question.lower())
    # Skip same-day and past markets: prices already reflect intraday observations
    # that the ensemble forecast does not see, producing spurious edges.
    if not target_date or target_date <= date.today():
        return None

    outcome_prices = market_data.get("outcomePrices", [])
    if isinstance(outcome_prices, str):
        import json
        try:
            outcome_prices = json.loads(outcome_prices)
        except Exception:
            outcome_prices = []
    if not outcome_prices or len(outcome_prices) < 2:
        return None

    try:
        yes_price = float(outcome_prices[0])
        no_price = float(outcome_prices[1])
    except (ValueError, IndexError):
        return None

    if market_data.get("closed", False):
        return None
    if yes_price > 0.98 or yes_price < 0.005:
        return None

    volume = float(market_data.get("volume", 0) or 0)

    return WeatherMarket(
        slug=event_slug,
        market_id=str(market_data.get("id", "")),
        platform="polymarket",
        title=question,
        city_key=city_key,
        city_name=city_name,
        target_date=target_date,
        threshold_f=_celsius_to_fahrenheit(degrees_c),
        metric="high",
        direction="at_or_below" if is_floor else "equal",
        yes_price=yes_price,
        no_price=no_price,
        volume=volume,
        unit="C",
        bucket_type="floor" if is_floor else "equality",
        bucket_center_c=degrees_c,
        event_id=event_id,
    )


def _parse_polymarket_weather(
    market_data: dict,
    event_slug: str,
    city_keys: Optional[List[str]] = None,
) -> Optional[WeatherMarket]:
    """Parse a Polymarket market dict into a WeatherMarket if it's a temp market."""
    question = market_data.get("question", "") or market_data.get("groupItemTitle", "")
    if not question:
        return None

    parsed = _parse_weather_market_title(question)
    if not parsed:
        return None

    # Filter by requested cities
    if city_keys and parsed["city_key"] not in city_keys:
        return None

    # Only trade markets for dates in the future (or today)
    if parsed["target_date"] < date.today():
        return None

    # Parse prices
    outcome_prices = market_data.get("outcomePrices", [])
    if isinstance(outcome_prices, str):
        import json
        try:
            outcome_prices = json.loads(outcome_prices)
        except Exception:
            outcome_prices = []

    if not outcome_prices or len(outcome_prices) < 2:
        return None

    try:
        yes_price = float(outcome_prices[0])
        no_price = float(outcome_prices[1])
    except (ValueError, IndexError):
        return None

    # Skip resolved markets
    if market_data.get("closed", False):
        return None
    if yes_price > 0.98 or yes_price < 0.02:
        return None

    volume = float(market_data.get("volume", 0) or 0)

    return WeatherMarket(
        slug=event_slug,
        market_id=str(market_data.get("id", "")),
        platform="polymarket",
        title=question,
        city_key=parsed["city_key"],
        city_name=parsed["city_name"],
        target_date=parsed["target_date"],
        threshold_f=parsed["threshold_f"],
        metric=parsed["metric"],
        direction=parsed["direction"],
        yes_price=yes_price,
        no_price=no_price,
        volume=volume,
    )
