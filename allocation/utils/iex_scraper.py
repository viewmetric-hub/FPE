"""
Scrape MCP prediction table from ViewMetric IEX Predictor.
Uses the same query parameters as the site UI (?interval=15min&delivery_period=...&apply=true)
so Actual MCP is present when the delivery day has cleared (e.g. yesterday).
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

VIEWMETRIC_IEX_BASE = "https://viewmetric.in/iex-predictor/"

# ViewMetric UI uses these delivery_period values (see site dropdown / URL).
ALLOWED_DELIVERY_PERIODS = frozenset({"yesterday", "today", "tomorrow"})

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _parse_price_cell(text: str) -> float | None:
    t = (text or "").strip()
    if not t or t in ("—", "-", "–", "NA", "N/A", "n/a"):
        return None
    t = re.sub(r"[₹$,]", "", t)
    t = re.sub(r"\s+", "", t)
    t = re.sub(r"(?i)^rs\.?", "", t)
    try:
        return float(t)
    except ValueError:
        return None


def _parse_block_cell(text: str) -> int | None:
    t = (text or "").strip()
    if not t:
        return None
    try:
        return int(t)
    except ValueError:
        return None


def _normalize_delivery_period(raw: str | None) -> str:
    if not raw:
        return "yesterday"
    key = raw.strip().lower().replace(" ", "_")
    if key in ALLOWED_DELIVERY_PERIODS:
        return key
    return "yesterday"


def fetch_iex_predictions(
    delivery_period: str | None = "yesterday",
    *,
    timeout: int = 45,
) -> list[dict]:
    """
    Fetch slot rows from ViewMetric. Actual MCP is populated when the site has data
    for that delivery period (typically ``yesterday``); ``today`` may still be dashes.
    """
    period = _normalize_delivery_period(delivery_period)

    params = {
        "interval": "15min",
        "delivery_period": period,
        "apply": "true",
    }
    url = f"{VIEWMETRIC_IEX_BASE}?{urlencode(params)}"

    response = requests.get(
        url,
        headers=DEFAULT_HEADERS,
        timeout=timeout,
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    # Only the first <table> is the 15-min snapshot; the page may include a second table (e.g. summary).
    table = soup.find("table")
    if table is None:
        raise ValueError("No table found on ViewMetric IEX Predictor page (layout may have changed).")

    rows = table.select("tbody tr")
    if not rows:
        rows = [r for r in table.find_all("tr") if not r.find("th")]

    data: list[dict] = []
    for row in rows:
        cols = row.find_all("td")
        if len(cols) < 4:
            continue

        time_block = cols[0].get_text(strip=True)
        block = _parse_block_cell(cols[1].get_text()) if len(cols) > 1 else None
        predicted_mcp = _parse_price_cell(cols[2].get_text()) if len(cols) > 2 else None
        actual_raw = cols[3].get_text(strip=True) if len(cols) > 3 else ""
        actual_mcp = _parse_price_cell(actual_raw)

        price_level = cols[4].get_text(strip=True) if len(cols) > 4 else ""
        confidence = cols[5].get_text(strip=True) if len(cols) > 5 else ""

        if predicted_mcp is None:
            continue

        data.append(
            {
                "time_block": time_block,
                "block": block,
                "predicted_mcp": predicted_mcp,
                "actual_mcp": actual_mcp,
                "price_level": price_level,
                "confidence": confidence,
            }
        )

    logger.info("Scraped sample (first 5 rows): %s", data[:5])

    if not data:
        raise ValueError("Parsed zero data rows from ViewMetric table.")

    if all(r.get("actual_mcp") is None for r in data):
        logger.warning(
            "Actual MCP not loaded for delivery_period=%s — check apply=true, headers, or JS-only rendering",
            period,
        )

    return data
