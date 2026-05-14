"""
IEX price prediction using explainable slot-wise averages.
Predicts next-day MCP for 96 slots from historical slot-wise averages.
No ML — simple, interpretable logic.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Dict, Optional

from django.utils import timezone

from allocation.models import IexGreenDayAheadMcpSlot
from allocation.slot_utils import SLOTS_PER_DAY

# Default lookback: last N days for slot-wise average
DEFAULT_LOOKBACK_DAYS = 7


def get_historical_slot_prices(
    target_date: datetime.date,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> Dict[int, list]:
    """
    Fetch last N days of MCP for each slot.
    Source 1: IexGreenDayAheadMcpSlot (DB cache from IEX fetches).
    Source 2: Excel files (fallback) from data_loader.
    Returns {slot_index: [price1, price2, ...]} for dates before target_date.
    """
    start_date = target_date - datetime.timedelta(days=lookback_days)
    qs = (
        IexGreenDayAheadMcpSlot.objects.filter(
            date__gte=start_date,
            date__lt=target_date,
        )
        .order_by("date", "slot_index")
    )

    slot_prices: Dict[int, list] = {i: [] for i in range(1, SLOTS_PER_DAY + 1)}
    for row in qs:
        idx = int(row.slot_index)
        if 1 <= idx <= SLOTS_PER_DAY:
            val = float(row.mcp_rs_per_mwh)
            if val > 0:  # Skip zeros (bad/missing data)
                slot_prices[idx].append(val)

    # Fallback: Excel loader if DB has little/no data
    total_points = sum(len(v) for v in slot_prices.values())
    if total_points < 96:  # Less than one full day of data
        try:
            from .data_loader import load_all_data, DEFAULT_DATA_PATH

            df = load_all_data(DEFAULT_DATA_PATH)
            if not df.empty:
                for _, r in df.iterrows():
                    d = r.get("date")
                    if d is None:
                        continue
                    d = d.date() if hasattr(d, "date") else d
                    if start_date <= d < target_date:
                        idx = int(r["slot_index"])
                        val = float(r["mcp"])
                        if 1 <= idx <= SLOTS_PER_DAY and val > 0:
                            slot_prices[idx].append(val)
        except Exception:
            pass

    return slot_prices


def predict_next_day_prices(
    target_date: Optional[datetime.date] = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    use_random_variation: bool = False,
) -> Dict[int, Decimal]:
    """
    Predict MCP (Rs/MWh) for 96 slots using historical slot-wise averages.

    Logic:
    1. Fetch last N days of data for each slot
    2. For each slot: predicted_price = average(slot_prices)
    3. Optional: add small ±0.1 Rs/kWh (~±100 Rs/MWh) variation

    Returns {slot_index: mcp_rs_per_mwh}.
    """
    import random

    if target_date is None:
        target_date = timezone.localdate()

    slot_prices = get_historical_slot_prices(target_date, lookback_days)

    # Time-of-day default profile when no history (realistic: morning moderate, afternoon low, evening high)
    # Rs/MWh: slot 1-24 (~night): 4000, 25-48 (~morning): 4500, 49-72 (~afternoon solar): 2500, 73-96 (~evening): 5500
    def _default_for_slot(slot: int) -> float:
        hour = (slot - 1) // 4 + 1
        if 1 <= hour <= 6:
            return 4000.0
        if 7 <= hour <= 10:
            return 4500.0
        if 11 <= hour <= 17:
            return 2500.0  # Solar effect
        return 5500.0  # Evening peak

    predictions: Dict[int, Decimal] = {}
    for slot in range(1, SLOTS_PER_DAY + 1):
        prices = slot_prices.get(slot, [])
        if prices:
            avg_price = sum(prices) / len(prices)
        else:
            avg_price = _default_for_slot(slot)

        if use_random_variation:
            # ±100 Rs/MWh (~±0.1 Rs/kWh)
            variation = random.uniform(-100, 100)
            avg_price = max(500, avg_price + variation)

        predictions[slot] = Decimal(str(round(avg_price, 2)))

    return predictions
