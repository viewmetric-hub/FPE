"""
Shared context for consumer allocation recommendations and slot-level approve APIs.
"""

import datetime
from decimal import Decimal

from django.utils import timezone

from allocation.ai_allocator import compute_allocation_with_ai_overrides
from allocation.iex_service import ensure_iex_mcp_for_date
from allocation.models import ConsumerGeneratorAllocationOverride, ConsumerGeneratorAllocationRun, GeneratorSupplySchedule
from core.api.serializers import get_or_none_transmission_loss
from core.models import Plant
from core.tariff_utils import normalize_plant_tariff_difference

IEX_CONTRACT_TARIFF_RS_PER_MWH = 3580


def build_slot_revision_unallocated_maps(schedule):
    """
    Per-slot AI pool (45% of generator supply) for each generator upload revision snapshot.
    Keys: submitted, upload_<n>, plus legacy revision1–3 aliases used by stored SlotAllocationApproval rows.
    """
    revisions = list(schedule.upload_revisions.order_by('revision_number'))
    current = {int(s.slot_index): float(s.supply_mwh) for s in schedule.slots.all()}
    submitted_supply = current.copy()
    for rev in reversed(revisions):
        for d in rev.deltas.all():
            submitted_supply[int(d.slot_index)] = float(d.previous_mwh)

    per_slot: dict[int, dict[str, float]] = {i: {} for i in range(1, 97)}
    # Per slot: which upload revision last changed the supply for that slot.
    # Used by CM UI so only actually-changed slots show "Revision N"; others stay "Submitted schedule".
    slot_default_revision_key: dict[int, str] = {i: 'submitted' for i in range(1, 97)}

    def write_pool(label: str, supply_map: dict[int, float]) -> None:
        for idx in range(1, 97):
            sup = float(supply_map.get(idx, 0.0))
            per_slot[idx][label] = float(round(Decimal(str(sup)) * Decimal('0.45'), 8))

    write_pool('submitted', submitted_supply)
    state = submitted_supply.copy()
    for rev in revisions:
        for d in rev.deltas.all():
            state[int(d.slot_index)] = float(d.new_mwh)
            slot_default_revision_key[int(d.slot_index)] = f'upload_{rev.revision_number}'
        write_pool(f'upload_{rev.revision_number}', state)

    max_n = revisions[-1].revision_number if revisions else 0
    for idx in range(1, 97):
        row = per_slot[idx]
        row.setdefault('submitted', 0.0)
        if max_n >= 1:
            row['revision1'] = row.get('upload_1', row['submitted'])
        else:
            row['revision1'] = row['submitted']
        row['revision2'] = row.get(f'upload_{max_n}', row['submitted']) if max_n else row['submitted']
        if max_n >= 3:
            row['revision3'] = row.get('upload_3', row['revision2'])
        else:
            row['revision3'] = row['revision2']

    rev_nums = [r.revision_number for r in revisions]
    return per_slot, rev_nums, slot_default_revision_key


def resolve_unallocated_for_revision(ctx: dict, slot_index: int, approved_revision: str | None) -> float:
    maps = ctx.get('slot_revision_unallocated') or {}
    idx = int(slot_index)
    key = (approved_revision or 'revision2').strip()
    row = maps.get(idx)
    if row and key in row:
        return float(row[key])
    return float(ctx['slot_index_to_unallocated'].get(idx, 0.0))


def validate_allocation_date(value: datetime.date) -> datetime.date:
    today = timezone.localdate()
    max_date = today + datetime.timedelta(days=6)
    if value < today:
        raise ValueError('Allocation is only available from today onwards.')
    if value > max_date:
        raise ValueError('You can only view for the next 7 days.')
    return value


def load_consumer_allocation_slot_context(consumer, req_date):
    """
    Returns plants (enriched), slot_rows, unallocated/total supply per slot, MCP, remaining AI need per plant/slot.
    """
    run = ConsumerGeneratorAllocationRun.objects.filter(consumer=consumer, date=req_date).first()
    overrides_map: dict[int, Decimal] = {}
    if run and run.status == ConsumerGeneratorAllocationRun.Status.APPROVED:
        overrides_map = {
            ov.plant_id: ov.ai_alloc_mwh_override_total
            for ov in ConsumerGeneratorAllocationOverride.objects.filter(run=run)
        }

    try:
        mcp_map = ensure_iex_mcp_for_date(req_date)
    except Exception:
        mcp_map = None

    allocation_result = compute_allocation_with_ai_overrides(
        consumer,
        req_date,
        overrides_map,
        mcp_by_slot_index=mcp_map,
    )
    demand_by_plant = allocation_result.get('demand_by_plant') or {}
    allocations = allocation_result.get('allocations') or {}
    plants = allocation_result['plants']
    slot_rows = allocation_result.get('slot_rows', [])

    def _slot_supply_mwh(row: dict) -> Decimal:
        v = row.get('supply_mwh')
        if v is not None and str(v) != '':
            return Decimal(str(v))
        return Decimal(str(row.get('allocated_mwh', '0')))

    base_total_by_plant_id: dict[int, Decimal] = {}
    for p in plants:
        pid = int(p.get('plant_id') or 0)
        base_total_by_plant_id[pid] = Decimal(str(p.get('base_total_mwh') or 0))

    year = req_date.year
    plant_objs = {int(p.id): p for p in Plant.objects.filter(consumer=consumer)}
    for p in plants:
        pid = int(p.get('plant_id') or 0)
        p['allocated_total_mwh'] = str(base_total_by_plant_id.get(pid, Decimal('0')))
        plant_obj = plant_objs.get(p['plant_id'])
        tpl = get_or_none_transmission_loss(plant_obj, year) if plant_obj else None
        p['state_transition_loss_percent'] = str(tpl.state_transition_loss_percent) if tpl else '0'
        p['central_transmission_loss_percent'] = str(tpl.central_transmission_loss_percent) if tpl else '0'
        if plant_obj:
            htd = plant_obj.hourly_tariff_difference or []
            if htd:
                p['hourly_tariff_difference'] = normalize_plant_tariff_difference(htd)
            else:
                spread = float(plant_obj.grid_tariff_per_unit or 0) - float(plant_obj.re_tariff_per_unit or 0)
                p['hourly_tariff_difference'] = [spread] * 96
        else:
            p['hourly_tariff_difference'] = [0.0] * 96

    slot_index_to_unallocated: dict[int, float] = {}
    slot_index_to_total_supply_mwh: dict[int, float] = {}
    slot_index_to_mcp: dict[int, float] = {}
    for s in slot_rows:
        idx = int(s['slot_index'])
        sup = _slot_supply_mwh(s)
        gen = float(sup)
        slot_index_to_total_supply_mwh[idx] = gen
        slot_index_to_unallocated[idx] = float(sup * Decimal('0.45'))
    if mcp_map:
        for idx, mcp_val in mcp_map.items():
            slot_index_to_mcp[int(idx)] = float(mcp_val)

    remaining_need_by_plant_slot: dict[int, dict[int, float]] = {}
    for p in plants:
        pid = p['plant_id']
        remaining_need_by_plant_slot[pid] = {}
        for slot_idx in range(1, 97):
            demand_val = float(demand_by_plant.get(pid, {}).get(slot_idx, 0) or 0)
            base_val = float(allocations.get(slot_idx, {}).get(pid, {}).get('base', 0) or 0)
            remaining_need_by_plant_slot[pid][slot_idx] = max(0, demand_val - base_val)

    slot_revision_unallocated: dict[int, dict[str, float]] = {}
    generator_revision_numbers: list[int] = []
    slot_default_revision_key: dict[int, str] = {}
    schedule = GeneratorSupplySchedule.objects.filter(consumer=consumer, date=req_date).first()
    if schedule:
        slot_revision_unallocated, generator_revision_numbers, slot_default_revision_key = build_slot_revision_unallocated_maps(
            schedule
        )

    return {
        'plants': plants,
        'slot_rows': slot_rows,
        'slot_index_to_unallocated': slot_index_to_unallocated,
        'slot_index_to_total_supply_mwh': slot_index_to_total_supply_mwh,
        'slot_index_to_mcp': slot_index_to_mcp,
        'remaining_need_by_plant_slot': remaining_need_by_plant_slot,
        'allocations': allocations,
        'demand_by_plant': demand_by_plant,
        'mcp_map': mcp_map,
        'allocation_result': allocation_result,
        'run': run,
        'slot_revision_unallocated': slot_revision_unallocated,
        'generator_revision_numbers': generator_revision_numbers,
        'generator_supply_schedule': schedule,
        'slot_default_supply_revision_key': slot_default_revision_key,
    }
