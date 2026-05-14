from __future__ import annotations

import datetime
import re
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from bs4 import BeautifulSoup
from django.utils import timezone

import requests

from allocation.slot_utils import SLOTS_PER_DAY

IEX_GREEN_DAY_AHEAD_SNAPSHOT_URL = "https://www.iexindia.com/market-data/green-day-ahead-market/market-snapshot"


def _parse_date_from_text(text: str) -> Optional[datetime.date]:
    """
    Tries to parse a date in dd-mm-YYYY format from arbitrary text.
    """
    m = re.search(r"(\d{2})-(\d{2})-(\d{4})", text)
    if not m:
        return None
    dd, mm, yyyy = m.group(1), m.group(2), m.group(3)
    try:
        return datetime.date(int(yyyy), int(mm), int(dd))
    except ValueError:
        return None


def _is_slot_data_row(row_text: str) -> bool:
    """True if row looks like a 15-min slot data row (has time block e.g. 00:00 - 00:15)."""
    return bool(re.search(r"\d{2}:\d{2}\s*-\s*\d{2}:\d{2}", row_text))


def fetch_iex_green_day_ahead_mcp(date: Optional[datetime.date] = None) -> Tuple[datetime.date, Dict[int, Decimal]]:
    """
    Attempts to fetch MCP for 96 slots from IEX snapshot page.
    The IEX page UI may default to "Today" and not support arbitrary future dates without user interaction.

    Returns:
      (delivery_date, {slot_index: mcp})
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Python requests",
        "Accept": "text/html,application/xhtml+xml",
    }
    resp = requests.get(IEX_GREEN_DAY_AHEAD_SNAPSHOT_URL, headers=headers, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    # Find a table that includes the MCP header.
    tables = soup.find_all("table")
    target_table = None
    for t in tables:
        if t.get_text(" ", strip=True).find("MCP (Rs/MWh)") != -1:
            target_table = t
            break
    if target_table is None:
        raise RuntimeError("Could not locate MCP table in IEX snapshot HTML.")

    # Determine which column holds the MCP values.
    header_cells: List[str] = []
    header_row = target_table.find("tr")
    if header_row:
        header_cells = [c.get_text(" ", strip=True) for c in header_row.find_all(["th", "td"])]

    mcp_col_idx = None
    for i, cell in enumerate(header_cells):
        if "MCP" in cell and "Rs/MWh" in cell:
            mcp_col_idx = i
            break

    # If header parsing failed, fall back to a best-effort assumption:
    # Known layout: [Date, Hour, Time Block, Purchase Bid, Sell Bid, MCV, Final Scheduled Volume, MCP, ...]
    if mcp_col_idx is None:
        mcp_col_idx = 7

    parsed_date: Optional[datetime.date] = None
    mcp_by_slot: Dict[int, Decimal] = {}

    rows = target_table.find_all("tr")
    slot_idx = 1
    for row in rows:
        cells = row.find_all(["td", "th"])
        if not cells or len(cells) < 2:
            continue

        row_text = row.get_text(" ", strip=True)
        if not _is_slot_data_row(row_text):
            continue  # Skip header/sub-header rows

        if parsed_date is None:
            parsed_date = _parse_date_from_text(row_text)

        if slot_idx > SLOTS_PER_DAY:
            break

        # IEX data rows have many sub-columns; MCP is always the last column.
        mcp_cell_idx = min(mcp_col_idx, len(cells) - 1) if mcp_col_idx < len(cells) else len(cells) - 1
        if len(cells) > 8:
            # Header has ~8 cols; data rows have 17+ (Purchase/Sell/MCV/FSV sub-cols). Use last col.
            mcp_cell_idx = len(cells) - 1
        if mcp_cell_idx >= 0:
            mcp_text = cells[mcp_cell_idx].get_text(" ", strip=True)
            # MCP might be empty or '-' depending on export modes.
            if mcp_text and mcp_text not in {"-", "—"}:
                try:
                    mcp_by_slot[slot_idx] = Decimal(mcp_text.replace(",", ""))
                except Exception:
                    mcp_by_slot[slot_idx] = Decimal("0")
            else:
                mcp_by_slot[slot_idx] = Decimal("0")

        slot_idx += 1

    if parsed_date is None:
        parsed_date = timezone.localdate()

    if date and parsed_date != date:
        # We still return what we found, but callers can decide whether to accept it.
        pass

    if len(mcp_by_slot) < SLOTS_PER_DAY:
        # Ensure full map.
        for i in range(1, SLOTS_PER_DAY + 1):
            mcp_by_slot.setdefault(i, Decimal("0"))

    return parsed_date, mcp_by_slot

