from __future__ import annotations

import datetime
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from django.db.models import Sum
from django.utils import timezone

from allocation.slot_utils import generate_day_slots
from core.models import Consumer, Plant
from core.api.serializers import get_or_none_transmission_loss, net_to_gross_additive
from allocation.models import (
    DemandSlot,
    GeneratorSupplySchedule,
    GeneratorSupplySlot,
)
from core.tariff_utils import tariff_diff_for_slot


BASE_PORTION = Decimal("0.55")
AI_PORTION = Decimal("0.45")


@dataclass(frozen=True)
class PlantDayTotals:
    plant_id: int
    plant_name: str
    grid_tariff_per_unit: Decimal
    re_tariff_per_unit: Decimal
    demand_total_mwh: Decimal
    base_total_mwh: Decimal
    ai_recommended_total_mwh: Decimal
    ai_final_total_mwh: Decimal
    allocated_total_mwh: Decimal
    ai_override_total_mwh: Decimal | None
    savings_weight: Decimal


def _get_consumer_plants(consumer: Consumer) -> list[Plant]:
    return list(Plant.objects.filter(consumer=consumer).order_by("id"))


def _get_demand_by_plant_and_slot(consumer: Consumer, date: datetime.date) -> dict[int, dict[int, Decimal]]:
    """
    Returns: demand_by_plant_id[plant_id][slot_index] = demand_mwh (net at plant)
    """
    qs = (
        DemandSlot.objects.filter(schedule__plant__consumer=consumer, schedule__date=date)
        .values("schedule__plant_id", "slot_index")
        .annotate(total=Sum("demand_mw"))
    )
    out: dict[int, dict[int, Decimal]] = defaultdict(dict)
    for row in qs:
        plant_id = int(row["schedule__plant_id"])
        slot_index = int(row["slot_index"])
        out[plant_id][slot_index] = row["total"] or Decimal("0")
    return out


def _get_demand_gross_by_plant_and_slot(
    consumer: Consumer, date: datetime.date, year: int | None = None
) -> dict[int, dict[int, Decimal]]:
    """
    Returns: demand_gross_by_plant_id[plant_id][slot_index] = demand_mwh (gross, with transmission loss)
    Uses additive formula (state + central) to match Demand Entry totals.
    """
    if year is None:
        year = timezone.localdate().year
    demand_net = _get_demand_by_plant_and_slot(consumer, date)
    plants = _get_consumer_plants(consumer)
    plant_by_id = {p.id: p for p in plants}
    out: dict[int, dict[int, Decimal]] = defaultdict(dict)
    for plant_id, slots in demand_net.items():
        plant = plant_by_id.get(plant_id)
        tpl = get_or_none_transmission_loss(plant, year) if plant else None
        state_pct = tpl.state_transition_loss_percent if tpl else Decimal("0")
        central_pct = tpl.central_transmission_loss_percent if tpl else Decimal("0")
        for slot_idx, net_val in slots.items():
            out[plant_id][slot_idx] = net_to_gross_additive(net_val, state_pct, central_pct)
    return out


def plant_demand_gross_mwh_total(consumer: Consumer, date: datetime.date, plant_id: int) -> Decimal:
    """Sum of gross demand entry MWh for one plant for one day (matches allocation demand basis)."""
    gross_by_plant = _get_demand_gross_by_plant_and_slot(consumer, date, year=date.year)
    slot_map = gross_by_plant.get(plant_id) or {}
    return sum(slot_map.values(), start=Decimal("0"))


def _get_supply_by_slot(consumer: Consumer, date: datetime.date) -> dict[int, Decimal]:
    qs = (
        GeneratorSupplySlot.objects.filter(schedule__consumer=consumer, schedule__date=date)
        .values("slot_index")
        .annotate(total=Sum("supply_mwh"))
    )
    out: dict[int, Decimal] = {}
    for row in qs:
        out[int(row["slot_index"])] = row["total"] or Decimal("0")
    return out


def _compute_recommended_allocations_for_date(
    consumer: Consumer,
    date: datetime.date,
    mcp_by_slot_index: dict[int, Decimal] | None = None,
) -> dict[str, Any]:
    plants = _get_consumer_plants(consumer)
    demand_net_by_plant = _get_demand_by_plant_and_slot(consumer, date)
    demand_by_plant = _get_demand_gross_by_plant_and_slot(consumer, date)  # gross for allocation
    supply_by_slot = _get_supply_by_slot(consumer, date)
    mcp_by_slot_index = mcp_by_slot_index or {}

    # Base split weights: proportionate to each plant's day gross demand
    # (proxy for average/max-load based bifurcation). Fallback to equal split when demand is unavailable.
    day_demand_total_by_plant: dict[int, Decimal] = {}
    total_day_demand = Decimal("0")
    for p in plants:
        pid = p.id
        plant_day_demand = sum((demand_by_plant.get(pid) or {}).values(), start=Decimal("0"))
        day_demand_total_by_plant[pid] = plant_day_demand
        total_day_demand += plant_day_demand

    if total_day_demand > 0 and plants:
        base_share_by_plant = {
            p.id: (day_demand_total_by_plant[p.id] / total_day_demand) for p in plants
        }
    else:
        n_plants = max(len(plants), 1)
        base_share_by_plant = {p.id: (Decimal("1") / Decimal(n_plants)) for p in plants}

    # Per-slot allocations for each plant:
    # allocations[slot_index][plant_id] = {'base': Decimal, 'ai': Decimal}
    allocations: dict[int, dict[int, dict[str, Decimal]]] = defaultdict(lambda: defaultdict(dict))

    plant_day = {p.id: {"demand": Decimal("0"), "base": Decimal("0"), "ai": Decimal("0")} for p in plants}

    for slot in generate_day_slots():
        idx = int(slot["slot_index"])
        supply_slot = supply_by_slot.get(idx, Decimal("0"))

        demand_slot_by_plant: dict[int, Decimal] = {}
        total_demand_slot = Decimal("0")
        for p in plants:
            d = demand_by_plant.get(p.id, {}).get(idx, Decimal("0")) or Decimal("0")
            demand_slot_by_plant[p.id] = d
            total_demand_slot += d

        # If there's no demand, allocations are all zeros.
        if total_demand_slot <= 0:
            continue

        # Base (55% of generator supply for this slot) split across plants using day-demand shares.
        # Pool = supply_slot × 55%; each plant gets pool × plant_share.
        base_pool = supply_slot * BASE_PORTION

        base_alloc: dict[int, Decimal] = {}
        sum_base = Decimal("0")
        for p in plants:
            pid = p.id
            b = base_pool * base_share_by_plant.get(pid, Decimal("0"))
            base_alloc[pid] = b
            sum_base += b

        remaining_supply_for_ai = max(supply_slot - sum_base, Decimal("0"))
        remaining_need_ai_by_plant: dict[int, Decimal] = {}
        for p in plants:
            # Plant can't receive more AI than it has beyond its base allocation.
            remaining_need_ai_by_plant[p.id] = max(demand_slot_by_plant[p.id] - base_alloc[p.id], Decimal("0"))

        # Profit-weighted greedy for AI portion:
        # If MCP is available: score by (mcp - re_tariff) per slot.
        # Otherwise: use slot-wise tariff difference when available, else (grid - re) fallback.
        mcp_for_slot = mcp_by_slot_index.get(idx)
        if mcp_for_slot is not None:
            def _sort_key(p: Plant):
                profit_margin = (mcp_for_slot - p.re_tariff_per_unit)
                return (profit_margin, demand_slot_by_plant[p.id])
        else:
            def _sort_key(p: Plant):
                htd = getattr(p, "hourly_tariff_difference", None) or []
                savings_proxy = Decimal(str(tariff_diff_for_slot(htd, idx)))
                if not htd:
                    savings_proxy = p.grid_tariff_per_unit - p.re_tariff_per_unit
                return (savings_proxy, demand_slot_by_plant[p.id])

        sorted_plants = sorted(plants, key=_sort_key, reverse=True)

        ai_alloc: dict[int, Decimal] = {p.id: Decimal("0") for p in plants}
        remaining_ai_supply = remaining_supply_for_ai
        for p in sorted_plants:
            if remaining_ai_supply <= 0:
                break
            pid = p.id
            take = min(remaining_ai_supply, remaining_need_ai_by_plant[pid])
            ai_alloc[pid] = take
            remaining_ai_supply -= take

        # Store for this slot and update day totals.
        for p in plants:
            pid = p.id
            d = demand_slot_by_plant[pid]
            b = base_alloc[pid]
            a = ai_alloc[pid]
            allocations[idx][pid] = {"base": b, "ai": a}
            plant_day[pid]["demand"] += d
            plant_day[pid]["base"] += b
            plant_day[pid]["ai"] += a

    return {
        "plants": plants,
        "allocations": allocations,
        "plant_day": plant_day,
        "demand_by_plant": demand_by_plant,
        "supply_by_slot": supply_by_slot,
    }


def compute_allocation_with_ai_overrides(
    consumer: Consumer,
    date: datetime.date,
    ai_override_total_by_plant_id: dict[int, Decimal] | None = None,
    mcp_by_slot_index: dict[int, Decimal] | None = None,
) -> dict[str, Any]:
    """
    Computes:
      - demand vs allocated per slot (overall)
      - plant-wise base: 55% of *generator supply* per slot, split by plant demand-share weights;
        AI portion uses remaining supply (greedy by margin vs IEX).
    """
    if ai_override_total_by_plant_id is None:
        ai_override_total_by_plant_id = {}

    provided_override_total_by_plant_id = dict(ai_override_total_by_plant_id)

    recommended = _compute_recommended_allocations_for_date(consumer, date, mcp_by_slot_index=mcp_by_slot_index)
    plants: list[Plant] = recommended["plants"]
    allocations = recommended["allocations"]
    plant_day = recommended["plant_day"]
    demand_by_plant = recommended["demand_by_plant"]
    supply_by_slot = recommended["supply_by_slot"]

    # If a plant isn't present in overrides, keep its recommended AI totals.
    ai_recommended_day_total_by_plant_id: dict[int, Decimal] = {}
    for p in plants:
        ai_recommended_day_total_by_plant_id[p.id] = plant_day[p.id]["ai"]

    override_total_by_plant_id: dict[int, Decimal] = {}
    for p in plants:
        if p.id in ai_override_total_by_plant_id:
            override_total_by_plant_id[p.id] = ai_override_total_by_plant_id[p.id]
        else:
            override_total_by_plant_id[p.id] = ai_recommended_day_total_by_plant_id[p.id]

    # Early-exit: if all provided overrides match the greedy recommendation, keep greedy output.
    # This keeps the "Approve without changing values" UX stable.
    has_nontrivial_overrides = False
    for pid, provided_val in provided_override_total_by_plant_id.items():
        if pid in ai_recommended_day_total_by_plant_id:
            if provided_val != ai_recommended_day_total_by_plant_id[pid]:
                has_nontrivial_overrides = True
                break
        else:
            # If an unknown plant id is provided, treat as non-trivial.
            if provided_val != Decimal("0"):
                has_nontrivial_overrides = True
                break

    plant_slot_by_id: dict[int, list[dict[str, Any]]] = {p.id: [] for p in plants}

    if (not provided_override_total_by_plant_id) or (not has_nontrivial_overrides):
        slot_rows: list[dict[str, Any]] = []
        for slot in generate_day_slots():
            idx = int(slot["slot_index"])
            supply_slot = supply_by_slot.get(idx, Decimal("0"))
            total_demand_slot = Decimal("0")
            for p in plants:
                total_demand_slot += demand_by_plant.get(p.id, {}).get(idx, Decimal("0")) or Decimal("0")

            allocated_total_slot = Decimal("0")
            for p in plants:
                allocated_total_slot += allocations.get(idx, {}).get(p.id, {}).get("base", Decimal("0")) or Decimal("0")
                allocated_total_slot += allocations.get(idx, {}).get(p.id, {}).get("ai", Decimal("0")) or Decimal("0")

            slot_rows.append(
                {
                    "slot_index": idx,
                    "slot_time": slot["slot_time"].strftime("%H:%M"),
                    "time_block": slot["time_block"],
                    "demand_mwh": str(total_demand_slot),
                    "supply_mwh": str(supply_slot),
                    "allocated_mwh": str(allocated_total_slot),
                }
            )

            for p in plants:
                pid = p.id
                b = allocations.get(idx, {}).get(pid, {}).get("base", Decimal("0")) or Decimal("0")
                ai = allocations.get(idx, {}).get(pid, {}).get("ai", Decimal("0")) or Decimal("0")
                fg = b + ai
                plant_slot_by_id[pid].append(
                    {
                        "slot_index": idx,
                        "base_mwh": str(b),
                        "ai_mwh": str(ai),
                        "final_gross_mwh": str(fg),
                    }
                )

        plant_day_final: dict[int, dict[str, Decimal]] = {
            p.id: {"demand": plant_day[p.id]["demand"], "base": plant_day[p.id]["base"], "ai_final": plant_day[p.id]["ai"]}
            for p in plants
        }
    else:
        # Override-aware allocation:
        # Distribute each plant's target AI day total across slots proportionally to its per-slot remaining need
        # (demand beyond base), then enforce per-slot supply caps.
        target_ai_total_by_plant_id: dict[int, Decimal] = {}
        for p in plants:
            pid = p.id
            if pid in provided_override_total_by_plant_id:
                target_ai_total_by_plant_id[pid] = provided_override_total_by_plant_id[pid]
            else:
                target_ai_total_by_plant_id[pid] = ai_recommended_day_total_by_plant_id[pid]

        remaining_need_ai_total_by_plant_id: dict[int, Decimal] = {}
        for p in plants:
            remaining_need_ai_total_by_plant_id[p.id] = max(plant_day[p.id]["demand"] - plant_day[p.id]["base"], Decimal("0"))

        slot_rows = []
        plant_day_final = {
            p.id: {"demand": plant_day[p.id]["demand"], "base": plant_day[p.id]["base"], "ai_final": Decimal("0")}
            for p in plants
        }
        plant_slot_by_id = {p.id: [] for p in plants}

        for slot in generate_day_slots():
            idx = int(slot["slot_index"])
            supply_slot = supply_by_slot.get(idx, Decimal("0"))

            demand_slot_by_plant: dict[int, Decimal] = {}
            total_demand_slot = Decimal("0")
            for p in plants:
                pid = p.id
                d = demand_by_plant.get(pid, {}).get(idx, Decimal("0")) or Decimal("0")
                demand_slot_by_plant[pid] = d
                total_demand_slot += d

            sum_base = Decimal("0")
            base_by_plant: dict[int, Decimal] = {}
            for p in plants:
                pid = p.id
                base = allocations.get(idx, {}).get(pid, {}).get("base", Decimal("0")) or Decimal("0")
                base_by_plant[pid] = base
                sum_base += base

            remaining_supply_for_ai_slot = max(supply_slot - sum_base, Decimal("0"))

            raw_ai_by_plant: dict[int, Decimal] = {}
            sum_raw_ai = Decimal("0")
            for p in plants:
                pid = p.id
                demand_slot = demand_slot_by_plant[pid]
                remaining_need_ai_slot = max(demand_slot - base_by_plant[pid], Decimal("0"))
                denom = remaining_need_ai_total_by_plant_id.get(pid, Decimal("0"))

                if denom > 0 and remaining_need_ai_slot > 0:
                    raw_ai = target_ai_total_by_plant_id.get(pid, Decimal("0")) * (remaining_need_ai_slot / denom)
                else:
                    raw_ai = Decimal("0")

                raw_ai = min(raw_ai, remaining_need_ai_slot)
                raw_ai_by_plant[pid] = raw_ai
                sum_raw_ai += raw_ai

            # Enforce overall supply cap for the AI portion in this slot.
            if sum_raw_ai > remaining_supply_for_ai_slot and sum_raw_ai > 0:
                slot_scale = remaining_supply_for_ai_slot / sum_raw_ai
                for pid in list(raw_ai_by_plant.keys()):
                    raw_ai_by_plant[pid] = raw_ai_by_plant[pid] * slot_scale
                sum_raw_ai = sum(raw_ai_by_plant.values(), Decimal("0"))

            allocated_total_slot = sum_base + sum_raw_ai
            slot_rows.append(
                {
                    "slot_index": idx,
                    "slot_time": slot["slot_time"].strftime("%H:%M"),
                    "time_block": slot["time_block"],
                    "demand_mwh": str(total_demand_slot),
                    "supply_mwh": str(supply_slot),
                    "allocated_mwh": str(allocated_total_slot),
                }
            )

            for p in plants:
                plant_day_final[p.id]["ai_final"] += raw_ai_by_plant.get(p.id, Decimal("0"))

            for p in plants:
                pid = p.id
                b = base_by_plant[pid]
                ai = raw_ai_by_plant.get(pid, Decimal("0"))
                fg = b + ai
                plant_slot_by_id[pid].append(
                    {
                        "slot_index": idx,
                        "base_mwh": str(b),
                        "ai_mwh": str(ai),
                        "final_gross_mwh": str(fg),
                    }
                )

    # Produce plant day totals for UI.
    plant_totals: list[dict[str, Any]] = []
    for p in plants:
        pid = p.id
        demand_total_mwh = plant_day_final[pid]["demand"]
        base_total_mwh = plant_day_final[pid]["base"]
        ai_reco_total_mwh = plant_day[pid]["ai"]
        ai_final_total_mwh = plant_day_final[pid]["ai_final"]
        allocated_total_mwh = base_total_mwh + ai_final_total_mwh

        # "Savings" proxy using tariff spread. (IEX prediction is intentionally stubbed for MVP.)
        savings_weight = (p.grid_tariff_per_unit - p.re_tariff_per_unit)
        savings_estimate = ai_final_total_mwh * savings_weight

        override_val = ai_override_total_by_plant_id.get(pid)
        plant_totals.append(
            {
                "plant_id": pid,
                "plant_name": p.name,
                "grid_tariff_per_unit": str(p.grid_tariff_per_unit),
                "re_tariff_per_unit": str(p.re_tariff_per_unit),
                "demand_total_mwh": str(demand_total_mwh),
                "base_total_mwh": str(base_total_mwh),
                "ai_recommended_total_mwh": str(ai_reco_total_mwh),
                "ai_override_total_mwh": str(override_val) if override_val is not None else None,
                "ai_final_total_mwh": str(ai_final_total_mwh),
                "allocated_total_mwh": str(allocated_total_mwh),
                "savings_estimate": str(savings_estimate),
                "savings_weight": str(savings_weight),
                "iex_prediction_stub_mcp": None,
            }
        )

    plant_slot_allocations_out = [
        {
            "plant_id": p.id,
            "plant_name": p.name,
            "slots": plant_slot_by_id.get(p.id, []),
        }
        for p in plants
    ]

    return {
        "date": date,
        "plants": plant_totals,
        "slot_rows": slot_rows,
        "demand_by_plant": demand_by_plant,
        "allocations": allocations,
        "plant_slot_allocations": plant_slot_allocations_out,
    }

