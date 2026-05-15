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
    # US
    "new york city": "nyc",
    "new york": "nyc",
    "nyc": "nyc",
    "chicago": "chicago",
    "miami": "miami",
    "los angeles": "los_angeles",
    "la": "los_angeles",
    "austin": "austin",
    "atlanta": "atlanta",
    "seattle": "seattle",
    # China + HK
    "beijing": "beijing",
    "shanghai": "shanghai",
    "chongqing": "chongqing",
    "guangzhou": "guangzhou",
    "chengdu": "chengdu",
    "wuhan": "wuhan",
    "hong kong": "hong_kong",
    "hongkong": "hong_kong",
    "shenzhen": "shenzhen",
    # Europe
    "london": "london",
    "paris": "paris",
    "madrid": "madrid",
    "milan": "milan",
    "munich": "munich",
    "amsterdam": "amsterdam",
    "warsaw": "warsaw",
    "helsinki": "helsinki",
    "moscow": "moscow",
    "istanbul": "istanbul",
    "ankara": "ankara",
}

# Month name to number
MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


_NEG_INF = -1e9
_POS_INF = 1e9


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
    threshold_f: float       # Reference temp in Fahrenheit (display/sort)
    metric: str              # "high" or "low"
    direction: str           # "above"/"below" (binary) | "equal"/"at_or_below"/"at_or_above"/"between"
    yes_price: float         # Price of YES outcome (0-1)
    no_price: float          # Price of NO outcome (0-1)
    volume: float = 0.0
    closed: bool = False
    unit: str = "F"          # "F" or "C" — original quoted unit
    bucket_type: str = "binary"  # "binary" | "equality" | "floor" | "ceiling" | "range"
    bucket_low_f: float = _NEG_INF   # Inclusive lower bound in F
    bucket_high_f: float = _POS_INF  # Exclusive upper bound in F
    bucket_label: str = ""           # Pretty label for UI, e.g. "28C", "56-57F", "≤45F"
    event_id: Optional[str] = None
    # Polymarket CLOB token IDs for [YES, NO]. Required for live orders.
    clob_token_ids: Optional[List[str]] = None
    # Deprecated — kept for back-compat in API response; will mirror bucket center in C if applicable.
    bucket_center_c: Optional[float] = None


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


_EVENT_TITLE_CITY_RE = re.compile(
    r"^(?:highest|lowest)\s+temperature\s+in\s+([a-z ]+?)\s+on\s+",
    re.IGNORECASE,
)


def _event_matches_configured_city(title: str, city_keys: Optional[List[str]]) -> bool:
    """Cheap pre-filter on the event title: True only if this is a per-city
    highest/lowest temperature event for a configured city."""
    if not title:
        return False
    m = _EVENT_TITLE_CITY_RE.match(title.strip())
    if not m:
        return False
    city_key = CITY_ALIASES.get(m.group(1).strip().lower())
    if not city_key:
        return False
    if city_keys and city_key not in city_keys:
        return False
    return True


async def fetch_polymarket_weather_markets(city_keys: Optional[List[str]] = None) -> List[WeatherMarket]:
    """
    Fetch Polymarket weather temperature city markets.

    Uses gamma-api /events?tag_slug=weather with offset pagination — returns
    the full active weather event list (~200+ events) rather than the
    /public-search endpoint which is capped at 50 results.

    Only parses "Highest/Lowest temperature in {city} ... on {date}" events.
    Other weather-tagged events (hurricanes, earthquakes, global warming)
    are skipped by the parser.
    """
    markets: List[WeatherMarket] = []
    seen_ids: set = set()

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            for offset in (0, 100, 200, 300):
                try:
                    response = await client.get(
                        "https://gamma-api.polymarket.com/events",
                        params={
                            "closed": "false",
                            "limit": 100,
                            "tag_slug": "weather",
                            "offset": offset,
                        },
                    )
                    response.raise_for_status()
                    events = response.json()
                except Exception as e:
                    logger.debug(f"Weather events page offset={offset} failed: {e}")
                    continue

                if not events:
                    break

                for event in events:
                    # Pre-filter at the event level: skip anything that isn't a
                    # "highest/lowest temperature in {configured city}" event.
                    if not _event_matches_configured_city(event.get("title", ""), city_keys):
                        continue
                    event_slug = event.get("slug", "") or ""
                    event_id = str(event.get("id", "")) if event.get("id") else None
                    for market_data in event.get("markets", []):
                        market = _parse_polymarket_bucketed(market_data, event_slug, event_id, city_keys)
                        if market and market.market_id not in seen_ids:
                            markets.append(market)
                            seen_ids.add(market.market_id)

                if len(events) < 100:
                    break  # last page

    except Exception as e:
        logger.warning(f"Failed to fetch weather markets: {e}")

    logger.info(f"Found {len(markets)} weather temperature city markets")
    return markets


def _celsius_to_fahrenheit(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


# Bucketed-title regexes. Order matters: range / floor / ceiling must be tried
# before equality, because equality's regex would otherwise swallow the unit-bearing
# prefix of the more specific forms.
_PRE = r"^will the (highest|lowest) temperature in ([a-z ]+?) be\s+"
_DATE_SUFFIX = r"\s+on\s+"
_RANGE_RE = re.compile(_PRE + r"between\s+(-?\d+)\s*-\s*(-?\d+)\s*°\s*([cf])" + _DATE_SUFFIX, re.IGNORECASE)
_FLOOR_RE = re.compile(_PRE + r"(-?\d+)\s*°\s*([cf])\s+or below" + _DATE_SUFFIX, re.IGNORECASE)
_CEIL_RE = re.compile(_PRE + r"(-?\d+)\s*°\s*([cf])\s+or (?:higher|above)" + _DATE_SUFFIX, re.IGNORECASE)
_EQ_RE = re.compile(_PRE + r"(-?\d+)\s*°\s*([cf])" + _DATE_SUFFIX, re.IGNORECASE)


def _bucket_bounds_f(value: float, unit: str) -> tuple[float, float]:
    """Convert a single integer-degree value to a half-open Fahrenheit bucket
    [center-0.5, center+0.5) in the original unit, then map to F."""
    if unit.upper() == "C":
        return _celsius_to_fahrenheit(value - 0.5), _celsius_to_fahrenheit(value + 0.5)
    return value - 0.5, value + 0.5


def _parse_polymarket_bucketed(
    market_data: dict,
    event_slug: str,
    event_id: Optional[str],
    city_keys: Optional[List[str]] = None,
) -> Optional[WeatherMarket]:
    """
    Parse a Polymarket "Highest/Lowest temperature in CITY ... on DATE" market.

    Supported question shapes (case-insensitive):
      • equality   "Will the highest temperature in Beijing be 28°C on May 16?"
      • floor      "Will the lowest temperature in NYC be 45°F or below on May 14?"
      • ceiling    "Will the lowest temperature in Tokyo be 84°F or higher on May 16?"
      • range      "Will the highest temperature in Seattle be between 56-57°F on May 14?"
    """
    question = (market_data.get("question") or market_data.get("groupItemTitle") or "").strip()
    if not question:
        return None

    bucket_type: str
    metric: str
    city_phrase: str
    unit: str
    low_f: float
    high_f: float
    threshold_f: float
    label: str
    center_c: Optional[float] = None

    m = _RANGE_RE.match(question)
    if m:
        metric_raw, city_phrase, a_str, b_str, unit = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5).upper()
        a, b = float(a_str), float(b_str)
        if unit == "C":
            low_f = _celsius_to_fahrenheit(a - 0.5)
            high_f = _celsius_to_fahrenheit(b + 0.5)
        else:
            low_f = a - 0.5
            high_f = b + 0.5
        bucket_type = "range"
        threshold_f = (low_f + high_f) / 2
        label = f"{int(a)}-{int(b)}{unit}"
    else:
        m = _FLOOR_RE.match(question)
        if m:
            metric_raw, city_phrase, n_str, unit = m.group(1), m.group(2), m.group(3), m.group(4).upper()
            n = float(n_str)
            _, high_f = _bucket_bounds_f(n, unit)
            low_f = _NEG_INF
            bucket_type = "floor"
            threshold_f = _celsius_to_fahrenheit(n) if unit == "C" else n
            label = f"≤{int(n)}{unit}"
            center_c = n if unit == "C" else None
        else:
            m = _CEIL_RE.match(question)
            if m:
                metric_raw, city_phrase, n_str, unit = m.group(1), m.group(2), m.group(3), m.group(4).upper()
                n = float(n_str)
                low_f, _ = _bucket_bounds_f(n, unit)
                high_f = _POS_INF
                bucket_type = "ceiling"
                threshold_f = _celsius_to_fahrenheit(n) if unit == "C" else n
                label = f"≥{int(n)}{unit}"
                center_c = n if unit == "C" else None
            else:
                m = _EQ_RE.match(question)
                if not m:
                    return None
                metric_raw, city_phrase, n_str, unit = m.group(1), m.group(2), m.group(3), m.group(4).upper()
                n = float(n_str)
                low_f, high_f = _bucket_bounds_f(n, unit)
                bucket_type = "equality"
                threshold_f = _celsius_to_fahrenheit(n) if unit == "C" else n
                label = f"{int(n)}{unit}"
                center_c = n if unit == "C" else None

    metric = "high" if metric_raw.lower() == "highest" else "low"

    city_key = CITY_ALIASES.get(city_phrase.strip().lower())
    if not city_key:
        return None
    if city_keys and city_key not in city_keys:
        return None

    from backend.data.weather import CITY_CONFIG
    city_name = CITY_CONFIG.get(city_key, {}).get("name", city_phrase.strip().title())

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
    from backend.config import settings as _settings
    if volume < _settings.WEATHER_MIN_VOLUME:
        return None

    # CLOB token IDs (for live trading; safe to be None in sim mode)
    clob_token_ids: Optional[List[str]] = None
    raw_tokens = market_data.get("clobTokenIds")
    if raw_tokens:
        try:
            import json as _json
            parsed = _json.loads(raw_tokens) if isinstance(raw_tokens, str) else raw_tokens
            if isinstance(parsed, list) and len(parsed) >= 2:
                clob_token_ids = [str(parsed[0]), str(parsed[1])]
        except (ValueError, TypeError):
            pass

    direction_map = {
        "equality": "equal",
        "floor": "at_or_below",
        "ceiling": "at_or_above",
        "range": "between",
    }

    return WeatherMarket(
        slug=event_slug,
        market_id=str(market_data.get("id", "")),
        platform="polymarket",
        title=question,
        city_key=city_key,
        city_name=city_name,
        target_date=target_date,
        threshold_f=threshold_f,
        metric=metric,
        direction=direction_map[bucket_type],
        yes_price=yes_price,
        no_price=no_price,
        volume=volume,
        unit=unit,
        bucket_type=bucket_type,
        bucket_low_f=low_f,
        bucket_high_f=high_f,
        bucket_label=label,
        bucket_center_c=center_c,
        event_id=event_id,
        clob_token_ids=clob_token_ids,
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
