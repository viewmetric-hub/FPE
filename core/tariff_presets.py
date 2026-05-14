"""
Canonical 96-slot (15-min) grid − RE tariff presets (Rs/unit).
Used by management commands — values from string literals, no rounding step.
"""

from __future__ import annotations

from decimal import Decimal


def tariff_rs(s: str) -> float:
    """Exact Rs/unit from decimal string (no quantize)."""
    return float(Decimal(s))


# Dahej: 28×2.30, 16×3.15, 16×2.30, 16×3.15, 20×2.30
DAHEJ_SLOT_TARIFF = (
    [tariff_rs("2.30")] * 28
    + [tariff_rs("3.15")] * 16
    + [tariff_rs("2.30")] * 16
    + [tariff_rs("3.15")] * 16
    + [tariff_rs("2.30")] * 20
)

# Selaqui: 24×1.64, 12×5.41, 36×3.35, 12×5.41, 12×1.64
SELAQUI_SLOT_TARIFF = (
    [tariff_rs("1.64")] * 24
    + [tariff_rs("5.41")] * 12
    + [tariff_rs("3.35")] * 36
    + [tariff_rs("5.41")] * 12
    + [tariff_rs("1.64")] * 12
)

assert len(DAHEJ_SLOT_TARIFF) == 96
assert len(SELAQUI_SLOT_TARIFF) == 96
