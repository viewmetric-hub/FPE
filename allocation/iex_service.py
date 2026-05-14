from __future__ import annotations

import datetime
import logging
import time
from decimal import Decimal
from pathlib import Path
from typing import Dict

from django.db import transaction
from django.utils import timezone

from allocation.iex_client import fetch_iex_green_day_ahead_mcp
from allocation.models import IexGreenDayAheadMcpSlot
from allocation.slot_utils import SLOTS_PER_DAY, generate_day_slots
from allocation.utils.iex_scraper import ALLOWED_DELIVERY_PERIODS, fetch_iex_predictions

logger = logging.getLogger(__name__)

# In-process warm cache: avoids repeated network + DB rewrites for the same delivery date
# in a short window (many API handlers call `ensure_iex_mcp_for_date` back-to-back).
_MCP_WARM: dict[datetime.date, tuple[float, Dict[int, Decimal]]] = {}
_MCP_WARM_TTL_SEC = 300
_MCP_WARM_MAX_KEYS = 24


def _mcp_warm_set(req_date: datetime.date, mcp_by_slot: Dict[int, Decimal], t0: float) -> Dict[int, Decimal]:
    if len(_MCP_WARM) > _MCP_WARM_MAX_KEYS:
        for k, _t in sorted(_MCP_WARM.items(), key=lambda it: it[1][0])[:8]:
            _MCP_WARM.pop(k, None)
    _MCP_WARM[req_date] = (t0, mcp_by_slot)
    return mcp_by_slot


def _delivery_period_for_date(req_date: datetime.date) -> str | None:
    """
    ViewMetric scraper only supports yesterday / today / tomorrow relative to local today.
    """
    today = timezone.localdate()
    delta = (req_date - today).days
    if delta == -1:
        return "yesterday"
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    return None


def _mcp_rs_per_mwh_from_viewmetric_rows(rows: list[dict]) -> Dict[int, Decimal] | None:
    """
    Scraper returns predicted_mcp in ₹/kWh (table column). Pipeline uses ₹/MWh everywhere.
    """
    out: Dict[int, Decimal] = {}
    for i, r in enumerate(rows):
        block = r.get("block")
        if block is not None and 1 <= int(block) <= SLOTS_PER_DAY:
            idx = int(block)
        else:
            idx = i + 1
        pred_kwh = r.get("predicted_mcp")
        if pred_kwh is None:
            continue
        try:
            v = float(pred_kwh)
        except (TypeError, ValueError):
            continue
        out[idx] = Decimal(str(round(v * 1000.0, 4)))
    if len(out) < 90:
        return None
    for idx in range(1, SLOTS_PER_DAY + 1):
        out.setdefault(idx, Decimal("0"))
    return out


def _try_scraped_mcp_viewmetric(req_date: datetime.date) -> Dict[int, Decimal] | None:
    period = _delivery_period_for_date(req_date)
    if period is None or period not in ALLOWED_DELIVERY_PERIODS:
        return None
    try:
        # Keep request latency bounded for dashboards that call this path synchronously.
        rows = fetch_iex_predictions(delivery_period=period, timeout=12)
    except Exception as exc:
        logger.warning("ViewMetric MCP scrape failed for %s (%s): %s", req_date, period, exc)
        return None
    mcp = _mcp_rs_per_mwh_from_viewmetric_rows(rows)
    if mcp:
        logger.info("Using ViewMetric scraped MCP for %s (delivery_period=%s)", req_date, period)
    return mcp


def _replace_mcp_slots_for_date(delivery_date: datetime.date, mcp_by_slot: Dict[int, Decimal]) -> Dict[int, Decimal]:
    expected_slots = generate_day_slots()
    slot_time_by_index = {int(s["slot_index"]): s["slot_time"] for s in expected_slots}
    with transaction.atomic():
        IexGreenDayAheadMcpSlot.objects.filter(date=delivery_date).delete()
        bulk = []
        for idx in range(1, SLOTS_PER_DAY + 1):
            bulk.append(
                IexGreenDayAheadMcpSlot(
                    date=delivery_date,
                    slot_index=idx,
                    slot_time=slot_time_by_index[idx],
                    mcp_rs_per_mwh=mcp_by_slot.get(idx, Decimal("0")),
                )
            )
        IexGreenDayAheadMcpSlot.objects.bulk_create(bulk)
    qs = IexGreenDayAheadMcpSlot.objects.filter(date=delivery_date).order_by("slot_index")
    return {int(s.slot_index): s.mcp_rs_per_mwh for s in qs}


def _get_predicted_mcp_for_date(req_date: datetime.date) -> Dict[int, Decimal] | None:
    """
    Use ML predictor for MCP when live IEX data is unavailable (e.g. future dates).
    Returns None if prediction fails.
    """
    try:
        from allocation.iex_prediction import IexMcpPredictor

        model_path = Path(__file__).resolve().parent.parent / "data" / "iex_mcp_model.pkl"
        predictor = IexMcpPredictor(model_path=model_path)
        return predictor.predict(req_date)
    except Exception:
        return None


def ensure_iex_mcp_for_date(req_date: datetime.date) -> Dict[int, Decimal]:
    """
    Ensures MCP for a delivery date exists in DB.

    Priority:
      1. ViewMetric scraper (viewmetric.in/iex-predictor) for yesterday / today / tomorrow
         relative to local today — same source as IEX Predictor UI; values in ₹/kWh converted to ₹/MWh.
      2. Cached DB rows if present and healthy.
      3. IEX Green Day-Ahead fetch (past dates).
      4. ML predictor or default.

    Returns:
      slot_index -> mcp_rs_per_mwh
    """
    now = time.time()
    warm = _MCP_WARM.get(req_date)
    if warm and (now - warm[0]) < _MCP_WARM_TTL_SEC:
        return warm[1]

    scraped = _try_scraped_mcp_viewmetric(req_date)
    if scraped is not None:
        return _mcp_warm_set(req_date, _replace_mcp_slots_for_date(req_date, scraped), now)

    existing_count = IexGreenDayAheadMcpSlot.objects.filter(date=req_date).count()
    if existing_count >= SLOTS_PER_DAY:
        qs = IexGreenDayAheadMcpSlot.objects.filter(date=req_date).order_by("slot_index")
        cached = {int(s.slot_index): s.mcp_rs_per_mwh for s in qs}
        # Invalidate if mostly zeros (bad/stale data)
        nonzero = sum(1 for v in cached.values() if v and float(v) > 0)
        if nonzero >= 48:  # At least half have values
            return _mcp_warm_set(req_date, cached, now)
        IexGreenDayAheadMcpSlot.objects.filter(date=req_date).delete()

    today = timezone.localdate()
    use_prediction = req_date >= today  # Always use AI prediction for today and future dates

    if not use_prediction:
        try:
            delivery_date, mcp_by_slot = fetch_iex_green_day_ahead_mcp(req_date)
            if delivery_date == req_date and mcp_by_slot and len(mcp_by_slot) >= SLOTS_PER_DAY:
                use_prediction = False
            else:
                delivery_date = None
                mcp_by_slot = None
                use_prediction = True
        except Exception:
            delivery_date = None
            mcp_by_slot = None
            use_prediction = True
    else:
        delivery_date = None
        mcp_by_slot = None

    if use_prediction or delivery_date is None or not mcp_by_slot or len(mcp_by_slot) < SLOTS_PER_DAY:
        predicted = _get_predicted_mcp_for_date(req_date)
        if predicted:
            mcp_by_slot = predicted
            delivery_date = req_date
        else:
            delivery_date = req_date
            # Fallback when predictor fails: use reasonable default (typical MCP range)
            mcp_by_slot = {i: Decimal("4000") for i in range(1, SLOTS_PER_DAY + 1)}

    return _mcp_warm_set(req_date, _replace_mcp_slots_for_date(delivery_date, mcp_by_slot), now)

