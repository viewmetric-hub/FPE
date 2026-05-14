"""
IEX MCP predictor: predict slot-wise MCP for next day using explainable logic.
Uses historical slot-wise averages (no ML). Integrates with allocation AI.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from pathlib import Path
from typing import Dict

from allocation.slot_utils import SLOTS_PER_DAY

from .services import predict_next_day_prices


class IexMcpPredictor:
    """
    Predicts MCP (Rs/MWh) for 96 slots for a given date.
    Uses slot-wise average of last N days. Explainable, no ML.
    """

    def __init__(
        self,
        data_path: str | Path | None = None,
        model_path: str | Path | None = None,
        lookback_days: int = 7,
    ):
        self.data_path = Path(data_path) if data_path else None
        self.model_path = Path(model_path) if model_path else None
        self.lookback_days = lookback_days

    def predict(self, target_date: datetime.date) -> Dict[int, Decimal]:
        """
        Predict MCP for 96 slots for target_date.
        Logic: for each slot, average of last N days' prices.
        Returns {slot_index: mcp_rs_per_mwh}.
        """
        return predict_next_day_prices(
            target_date=target_date,
            lookback_days=self.lookback_days,
            use_random_variation=False,
        )

    def predict_tomorrow_and_day_after(
        self, from_date: datetime.date | None = None
    ) -> Dict[datetime.date, Dict[int, Decimal]]:
        """
        Predict MCP for tomorrow and day-after from from_date (default: today).
        Returns {date: {slot_index: mcp}}.
        """
        if from_date is None:
            from_date = datetime.date.today()
        tomorrow = from_date + datetime.timedelta(days=1)
        day_after = from_date + datetime.timedelta(days=2)
        return {
            tomorrow: self.predict(tomorrow),
            day_after: self.predict(day_after),
        }
