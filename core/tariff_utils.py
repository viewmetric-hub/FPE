"""
Plant tariff difference: one value per 15-minute market slot (96 per day).
Legacy data may store 24 hourly values; expand each hour to 4 identical slots.
"""

from __future__ import annotations

from decimal import Decimal

SLOT_TARIFF_LEN = 96


def coerce_tariff_scalar(v) -> float:
    """
    Convert a stored/API value to float without rounding away digits the user entered.
    Uses Decimal(str(...)) to reduce binary float noise (e.g. 1.639999… → 1.64).
    """
    if v is None:
        return 0.0
    try:
        return float(Decimal(str(v)))
    except (ArithmeticError, ValueError, TypeError):
        return 0.0


def normalize_plant_tariff_difference(htd: list | None) -> list[float]:
    """
    Return exactly SLOT_TARIFF_LEN values — no quantize/round; preserves stored digits.
    - 96 values: coerced only (Decimal(str(x)) for stability)
    - 24 values: each hour copied to 4 consecutive slots (legacy)
    - other: pad with 0 or truncate
    """
    if not htd:
        return [0.0] * SLOT_TARIFF_LEN
    if len(htd) == SLOT_TARIFF_LEN:
        return [coerce_tariff_scalar(x) for x in htd]
    if len(htd) == 24:
        out: list[float] = []
        for v in htd:
            rv = coerce_tariff_scalar(v)
            out.extend([rv] * 4)
        return out[:SLOT_TARIFF_LEN]
    out = [coerce_tariff_scalar(x) for x in htd[:SLOT_TARIFF_LEN]]
    while len(out) < SLOT_TARIFF_LEN:
        out.append(0.0)
    return out[:SLOT_TARIFF_LEN]


def tariff_diff_for_slot(htd: list | None, slot_index_1based: int) -> float:
    """Grid−RE tariff difference (Rs/unit) for slot 1..96."""
    norm = normalize_plant_tariff_difference(htd)
    i = max(0, min(slot_index_1based - 1, SLOT_TARIFF_LEN - 1))
    return float(norm[i])


def average_tariff_for_hour(htd: list | None, hour_1based: int) -> float:
    """Mean of the four 15-min slots in hour 1..24."""
    if not (1 <= hour_1based <= 24):
        return 0.0
    norm = normalize_plant_tariff_difference(htd)
    start = (hour_1based - 1) * 4
    chunk = norm[start : start + 4]
    return sum(chunk) / len(chunk) if chunk else 0.0
