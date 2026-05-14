import datetime
from collections import defaultdict
from decimal import Decimal

import requests

from django.db import transaction
from django.db.models import Max, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated

from allocation.slot_utils import SLOTS_PER_DAY, generate_day_slots, slot_index_to_time_block
from allocation.api.serializers import (
    DemandEntryCreateSerializer,
    GeneratorSupplySubmitSerializer,
    IexMcpResponseSerializer,
    IexMcpUpsertSerializer,
    SlotApproveSerializer,
    SlotApproveBulkSerializer,
    SlotRevokeSerializer,
    SlotOverrideApproveSerializer,
)
from allocation.models import (
    ConsumerDemandApproval,
    ConsumerGeneratorAllocationOverride,
    ConsumerGeneratorAllocationRun,
    DemandSchedule,
    DemandSlot,
    GeneratorScheduleApproval,
    GeneratorSupplySchedule,
    GeneratorSupplySlot,
    GeneratorSupplyUploadRevision,
    GeneratorSupplyUploadRevisionDelta,
    IexGreenDayAheadMcpSlot,
    SlotAllocationApproval,
)
from core.api.utils import get_managed_consumer
from core.models import Consumer, Plant, PlantUser
from core.permissions import IsPlantUser, IsConsumerManager, IsGenerator
from allocation.ai_allocator import (
    compute_allocation_with_ai_overrides,
    plant_demand_gross_mwh_total,
    _get_demand_gross_by_plant_and_slot,
)
from allocation.iex_client import fetch_iex_green_day_ahead_mcp
from allocation.iex_service import ensure_iex_mcp_for_date
from allocation.recommendation_context import (
    IEX_CONTRACT_TARIFF_RS_PER_MWH,
    load_consumer_allocation_slot_context,
    resolve_unallocated_for_revision,
)
from core.tariff_utils import average_tariff_for_hour, tariff_diff_for_slot
from allocation.utils.iex_scraper import ALLOWED_DELIVERY_PERIODS, VIEWMETRIC_IEX_BASE, fetch_iex_predictions


def get_plant_for_plant_user(user):
    profile = PlantUser.objects.get(user=user)
    return profile.plant


def aggregate_consumer_demand_mwh_by_slot(consumer: Consumer, req_date: datetime.date) -> dict[int, Decimal]:
    """
    Per-slot total demand MWh (net + state% + central% loss), same as consumer manager / generator demand API.
    """
    from django.db.models import Sum

    from core.api.serializers import get_or_none_transmission_loss

    year = req_date.year
    slot_totals: dict[int, Decimal] = {}
    qs = (
        DemandSlot.objects.filter(schedule__plant__consumer=consumer, schedule__date=req_date)
        .values('schedule__plant_id', 'slot_index')
        .annotate(total=Sum('demand_mw'))
    )
    plant_ids = {row['schedule__plant_id'] for row in qs}
    plants = {p.id: p for p in Plant.objects.filter(id__in=plant_ids)}
    for row in qs:
        idx = int(row['slot_index'])
        plant_id = row['schedule__plant_id']
        net_val = Decimal(str(row['total'] or 0))
        plant = plants.get(plant_id)
        tpl = get_or_none_transmission_loss(plant, year) if plant else None
        state_pct = float(tpl.state_transition_loss_percent) if tpl else 0
        central_pct = float(tpl.central_transmission_loss_percent) if tpl else 0
        total_loss_pct = Decimal(str(state_pct + central_pct))
        loss_mwh = net_val * total_loss_pct / Decimal('100')
        gross_val = net_val + loss_mwh
        slot_totals[idx] = slot_totals.get(idx, Decimal('0')) + gross_val
    return slot_totals


class DemandEntryView(APIView):
    permission_classes = [IsAuthenticated, IsPlantUser]

    def _validate_date_range(self, value: datetime.date) -> datetime.date:
        today = timezone.localdate()
        max_date = today + datetime.timedelta(days=30)
        if value < today:
            raise ValueError('Demand entry is only allowed from today onwards.')
        if value > max_date:
            raise ValueError('You can only schedule for the next 30 days.')
        return value

    def get(self, request):
        date_str = request.query_params.get('date')
        if not date_str:
            return Response({'detail': 'date query parameter is required.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            req_date = datetime.date.fromisoformat(date_str)
        except ValueError:
            return Response({'detail': 'Invalid date format. Use YYYY-MM-DD.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            self._validate_date_range(req_date)
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        try:
            plant = get_plant_for_plant_user(request.user)
        except Exception:
            return Response(
                {'detail': 'No plant assigned to your account. Contact your administrator.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        schedule = (
            DemandSchedule.objects.filter(plant=plant, date=req_date)
            .select_related('plant')
            .first()
        )

        exists = schedule is not None
        shutdown = bool(schedule.shutdown) if schedule else False

        consumer = plant.consumer
        approval = ConsumerDemandApproval.objects.filter(consumer=consumer, date=req_date).first()
        approved = approval is not None

        slot_map = {}
        if schedule:
            for s in schedule.slots.all():
                slot_map[s.slot_index] = s.demand_mw

        day_slots = generate_day_slots()
        resp_slots = [
            {
                'slot_index': slot['slot_index'],
                'slot_time': slot['slot_time'].strftime('%H:%M'),
                'time_block': slot['time_block'],
                'demand_mw': slot_map.get(slot['slot_index'], 0),
            }
            for slot in day_slots
        ]

        resp = {
            'date': req_date,
            'exists': exists,
            'schedule_id': schedule.id if schedule else None,
            'shutdown': shutdown,
            'approved': approved,
            'slots': resp_slots,
        }
        return Response(resp, status=status.HTTP_200_OK)

    @transaction.atomic
    def post(self, request):
        serializer = DemandEntryCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        plant = get_plant_for_plant_user(request.user)
        req_date = data['date']
        shutdown = data.get('shutdown', False)
        slots_in = data['slots']
        slots_by_index = {s['slot_index']: s['demand_mw'] for s in slots_in}

        schedule, created = DemandSchedule.objects.get_or_create(
            plant=plant,
            date=req_date,
            defaults={'shutdown': shutdown, 'created_by_user': request.user, 'updated_at': timezone.now()},
        )
        if not created:
            schedule.shutdown = shutdown
            schedule.updated_at = timezone.now()
            schedule.save(update_fields=['shutdown', 'updated_at'])

        # Replace all slots (simple & safe for an upsert-style UI).
        DemandSlot.objects.filter(schedule=schedule).delete()

        bulk = []
        day_slots = generate_day_slots()
        for slot in day_slots:
            idx = slot['slot_index']
            demand_val = 0 if shutdown else slots_by_index.get(idx, 0)
            bulk.append(
                DemandSlot(
                    schedule=schedule,
                    slot_index=idx,
                    slot_time=slot['slot_time'],
                    demand_mw=demand_val,
                )
            )
        DemandSlot.objects.bulk_create(bulk)

        return Response({'detail': 'Demand schedule saved successfully.'}, status=status.HTTP_200_OK)

    @transaction.atomic
    def delete(self, request):
        date_str = request.query_params.get('date')
        if not date_str:
            return Response({'detail': 'date query parameter is required.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            req_date = datetime.date.fromisoformat(date_str)
        except ValueError:
            return Response({'detail': 'Invalid date format. Use YYYY-MM-DD.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            self._validate_date_range(req_date)
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        plant = get_plant_for_plant_user(request.user)
        schedule = DemandSchedule.objects.filter(plant=plant, date=req_date).first()
        if not schedule:
            return Response({'detail': 'No schedule exists for this date.'}, status=status.HTTP_200_OK)
        schedule.delete()
        return Response({'detail': 'Demand schedule deleted.'}, status=status.HTTP_200_OK)


class PlantDashboardView(APIView):
    permission_classes = [IsAuthenticated, IsPlantUser]

    def get(self, request):
        # Simple MVP dashboard: totals for a given date (defaults to tomorrow).
        date_str = request.query_params.get('date')
        today = timezone.localdate()
        default_date = today + datetime.timedelta(days=1)
        req_date = default_date
        if date_str:
            try:
                req_date = datetime.date.fromisoformat(date_str)
            except ValueError:
                return Response({'detail': 'Invalid date format.'}, status=status.HTTP_400_BAD_REQUEST)

        plant = get_plant_for_plant_user(request.user)
        schedule = DemandSchedule.objects.filter(plant=plant, date=req_date).first()

        total_demand = 0
        if schedule:
            total_demand = sum((s.demand_mw for s in schedule.slots.all()), 0)

        # Allocated is shown only after overall allocation approval.
        energy_allocated_net = 0
        consumer = plant.consumer
        run = ConsumerGeneratorAllocationRun.objects.filter(consumer=consumer, date=req_date).first()
        approved = bool(run and run.status == ConsumerGeneratorAllocationRun.Status.APPROVED)
        try:
            from allocation.ai_allocator import compute_allocation_with_ai_overrides
            from core.api.serializers import get_transmission_loss_percent, gross_to_net

            if approved:
                result = compute_allocation_with_ai_overrides(consumer, req_date)
                for p in result.get('plants', []):
                    if p.get('plant_id') == plant.id:
                        alloc_gross = Decimal(p.get('allocated_total_mwh', 0) or 0)
                        loss_pct = get_transmission_loss_percent(plant, req_date.year)
                        energy_allocated_net = float(gross_to_net(alloc_gross, loss_pct))
                        break
        except Exception:
            pass

        return Response(
            {
                'date': req_date,
                'total_renewable_generation_mwh': 0,
                'total_demand_mwh': total_demand,
                'energy_allocated_mwh': energy_allocated_net,
                'shutdown': bool(schedule.shutdown) if schedule else False,
                'approved': approved,
            },
            status=status.HTTP_200_OK,
        )


class PlantDashboardWeekView(APIView):
    """
    Daily demand totals (net) and allocated (net) for Plant User for tomorrow..tomorrow+6.
    Demand = without transmission loss. Allocated = gross * (1 - loss%) = net at plant.
    """

    permission_classes = [IsAuthenticated, IsPlantUser]

    def get(self, request):
        plant = get_plant_for_plant_user(request.user)
        consumer = plant.consumer

        today = timezone.localdate()
        tomorrow = today + datetime.timedelta(days=1)
        dates = [tomorrow + datetime.timedelta(days=i) for i in range(7)]

        schedules = (
            DemandSchedule.objects.filter(plant=plant, date__in=dates)
            .prefetch_related('slots')
        )
        schedule_by_date = {s.date: s for s in schedules}

        from allocation.ai_allocator import compute_allocation_with_ai_overrides
        from core.api.serializers import get_transmission_loss_percent, gross_to_net

        alloc_by_date = {}
        for d in dates:
            try:
                run = ConsumerGeneratorAllocationRun.objects.filter(consumer=consumer, date=d).first()
                approved = bool(run and run.status == ConsumerGeneratorAllocationRun.Status.APPROVED)
                if approved:
                    result = compute_allocation_with_ai_overrides(consumer, d)
                    for p in result.get('plants', []):
                        if p.get('plant_id') == plant.id:
                            alloc_gross = Decimal(p.get('allocated_total_mwh', 0) or 0)
                            loss_pct = get_transmission_loss_percent(plant, d.year)
                            alloc_by_date[d] = float(gross_to_net(alloc_gross, loss_pct))
                            break
                else:
                    alloc_by_date[d] = 0
            except Exception:
                alloc_by_date[d] = 0
            if d not in alloc_by_date:
                alloc_by_date[d] = 0

        series = []
        for d in dates:
            schedule = schedule_by_date.get(d)
            shutdown = bool(schedule.shutdown) if schedule else False
            total_demand = 0
            if schedule and not shutdown:
                total_demand = sum((s.demand_mw for s in schedule.slots.all()), 0)

            series.append(
                {
                    'date': d.isoformat(),
                    'date_label': d.strftime('%a %d %b'),
                    'total_demand_mwh': str(total_demand),
                    'total_allocated_mwh': str(alloc_by_date.get(d, 0)),
                    'shutdown': shutdown,
                }
            )

        return Response({'series': series}, status=status.HTTP_200_OK)


class ConsumerManagerDemandEntrySummaryView(APIView):
    """
    Returns daily demand totals for each plant in the consumer managed by the user,
    for tomorrow..tomorrow+6.
    """

    permission_classes = [IsAuthenticated, IsConsumerManager]

    def _validate_date_range(self, value: datetime.date) -> datetime.date:
        today = timezone.localdate()
        tomorrow = today + datetime.timedelta(days=1)
        max_date = tomorrow + datetime.timedelta(days=6)
        if value < tomorrow:
            raise ValueError('Demand entry is only allowed from tomorrow onwards.')
        if value > max_date:
            raise ValueError('You can only schedule for the next 7 days.')
        return value

    def get(self, request):
        date_str = request.query_params.get('date')
        if not date_str:
            return Response({'detail': 'date query parameter is required.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            req_date = datetime.date.fromisoformat(date_str)
        except ValueError:
            return Response({'detail': 'Invalid date format. Use YYYY-MM-DD.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            self._validate_date_range(req_date)
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        consumer = getattr(request.user, 'managed_consumer', None)
        if consumer is None:
            return Response({'detail': 'Consumer not linked for this manager user.'}, status=status.HTTP_400_BAD_REQUEST)

        approval = ConsumerDemandApproval.objects.filter(consumer=consumer, date=req_date).first()
        approved = approval is not None
        gen_approval = GeneratorScheduleApproval.objects.filter(consumer=consumer, date=req_date).first()
        generator_schedule_approved = gen_approval is not None

        plants = Plant.objects.filter(consumer=consumer).order_by('id')
        total = 0
        results = []

        from core.api.serializers import get_or_none_transmission_loss

        year = req_date.year
        for plant in plants:
            schedule = DemandSchedule.objects.filter(plant=plant, date=req_date).first()
            exists = schedule is not None
            shutdown = bool(schedule.shutdown) if schedule else False
            plant_total_net = 0
            if schedule and not shutdown:
                plant_total_net = sum((s.demand_mw for s in schedule.slots.all()), 0)
            plant_total_net = float(plant_total_net)
            tpl = get_or_none_transmission_loss(plant, year)
            state_pct = float(tpl.state_transition_loss_percent) if tpl else 0
            central_pct = float(tpl.central_transmission_loss_percent) if tpl else 0
            total_loss_pct = state_pct + central_pct
            total_loss_mwh = plant_total_net * total_loss_pct / 100
            plant_total_with_loss = plant_total_net + total_loss_mwh
            total += plant_total_with_loss

            results.append(
                {
                    'plant_id': plant.id,
                    'plant_name': plant.name,
                    'exists': exists,
                    'shutdown': shutdown,
                    'total_demand_mwh': str(round(plant_total_with_loss, 4)),
                }
            )

        return Response(
            {
                'date': req_date,
                'plants': results,
                'total_demand_mwh': str(round(total, 4)),
                'approved': approved,
                'approved_at': approval.approved_at if approval else None,
                'generator_schedule_approved': generator_schedule_approved,
                'generator_schedule_approved_at': gen_approval.approved_at if gen_approval else None,
            },
            status=status.HTTP_200_OK,
        )


class PlantDashboardSlotsView(APIView):
    """
    Plant user slot-wise view for a date:
      - demand (net, as entered by plant user)
      - approved supply (net at plant) per 15-min slot
      - difference = demand - supply

    Supply is shown only meaningfully after Consumer Manager overall approval.
    """

    permission_classes = [IsAuthenticated, IsPlantUser]

    def get(self, request):
        date_str = request.query_params.get("date")
        if not date_str:
            return Response({"detail": "date query parameter is required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            req_date = datetime.date.fromisoformat(date_str)
        except ValueError:
            return Response({"detail": "Invalid date format. Use YYYY-MM-DD."}, status=status.HTTP_400_BAD_REQUEST)

        plant = get_plant_for_plant_user(request.user)
        consumer = plant.consumer

        # Demand (net)
        schedule = DemandSchedule.objects.filter(plant=plant, date=req_date).prefetch_related("slots").first()
        shutdown = bool(schedule.shutdown) if schedule else False
        demand_by_slot: dict[int, float] = {s.slot_index: float(s.demand_mw) for s in (schedule.slots.all() if schedule else [])}

        # Approval status + approved AI allocation per slot (gross MWh) for this plant
        run = ConsumerGeneratorAllocationRun.objects.filter(consumer=consumer, date=req_date).first()
        approved = bool(run and run.status == ConsumerGeneratorAllocationRun.Status.APPROVED)

        approved_ai_by_slot: dict[int, float] = {}
        approved_slot_index_set: set[int] = set()
        recommended_ai_by_slot: dict[int, float] = {}
        lock_cutoff_slot = 0
        if approved:
            qs = SlotAllocationApproval.objects.filter(consumer=consumer, date=req_date, plant=plant).values("slot_index").annotate(total=Sum("allocated_mwh"))
            approved_ai_by_slot = {int(r["slot_index"]): float(r["total"] or 0) for r in qs}
            approved_slot_index_set = set(
                SlotAllocationApproval.objects.filter(consumer=consumer, date=req_date)
                .values_list("slot_index", flat=True)
            )

            # Mirror CM "time-locked approved" behavior for today's past slots.
            if req_date == timezone.localdate():
                now_local = timezone.localtime()
                mins_now = now_local.hour * 60 + now_local.minute
                next_quarter_mins = ((mins_now // 15) + 1) * 15
                if next_quarter_mins > 24 * 60:
                    next_quarter_mins = 24 * 60
                lock_cutoff_slot = max(0, min(int(SLOTS_PER_DAY), next_quarter_mins // 15))

            # For time-locked-but-not-persisted slots, use current recommendation split for this plant.
            try:
                overrides_map = {
                    ov.plant_id: ov.ai_alloc_mwh_override_total
                    for ov in ConsumerGeneratorAllocationOverride.objects.filter(run=run).select_related("plant")
                }
                try:
                    mcp_map = ensure_iex_mcp_for_date(req_date)
                except Exception:
                    mcp_map = None
                alloc_result = compute_allocation_with_ai_overrides(consumer, req_date, overrides_map, mcp_by_slot_index=mcp_map)
                alloc_map = alloc_result.get("allocations") or {}
                for slot_idx in range(1, int(SLOTS_PER_DAY) + 1):
                    ai_val = ((alloc_map.get(slot_idx) or {}).get(plant.id) or {}).get("ai", Decimal("0"))
                    recommended_ai_by_slot[slot_idx] = float(ai_val or 0)
            except Exception:
                recommended_ai_by_slot = {}

        # Base per-slot (gross) uses the same interpretation as consumer UI "Base supply":
        # we must use the same base totals as the Consumer Manager "Plantwise Allocation" screen,
        # which are derived from generator supply schedule constraints.
        base_total_gross = 0.0
        loss_pct = 0.0
        try:
            ctx = load_consumer_allocation_slot_context(consumer, req_date)
            # IMPORTANT: Use ctx['plants'] (enriched) which matches the Consumer Manager UI.
            # In recommendation_context, allocated_total_mwh is set from weighted 55% base bifurcation.
            for p in (ctx.get("plants") or []):
                if int(p.get("plant_id") or 0) == int(plant.id):
                    # In consumer manager UI, "Base supply (MWh)" uses allocated_total_mwh.
                    base_total_gross = float(Decimal(str(p.get("allocated_total_mwh") or 0)))
                    # Use the same loss% fields sent to Consumer Manager UI (state + central).
                    loss_pct = float(Decimal(str(p.get("state_transition_loss_percent") or 0))) + float(
                        Decimal(str(p.get("central_transmission_loss_percent") or 0))
                    )
                    break
        except Exception:
            base_total_gross = 0.0

        base_gross_per_slot = base_total_gross / float(SLOTS_PER_DAY)

        loss_factor = 1.0 - (loss_pct / 100.0)

        slots_out = []
        for slot in generate_day_slots():
            idx = int(slot["slot_index"])
            demand_net = 0.0 if shutdown else float(demand_by_slot.get(idx, 0.0))
            slot_approved = bool(approved and (idx in approved_slot_index_set or idx <= lock_cutoff_slot))
            # If not approved, show supply as 0 (waiting for overall approval).
            base_net = (base_gross_per_slot * loss_factor) if slot_approved else 0.0
            if not slot_approved:
                ai_gross = 0.0
            elif idx in approved_slot_index_set:
                ai_gross = float(approved_ai_by_slot.get(idx, 0.0))
            else:
                ai_gross = float(recommended_ai_by_slot.get(idx, 0.0))
            ai_tx_loss = ai_gross * (loss_pct / 100.0)
            ai_net = ai_gross - ai_tx_loss

            supply_gross = (base_gross_per_slot + ai_gross) if slot_approved else 0.0
            supply_net = base_net + ai_net
            diff = demand_net - supply_net

            slots_out.append(
                {
                    "slot_index": idx,
                    "time_block": slot["time_block"],
                    "demand_net_mwh": round(demand_net, 4),
                    "approved_supply_gross_mwh": round(supply_gross, 4),
                    "base_net_mwh": round(base_net, 4),
                    "ai_gross_mwh": round(ai_gross, 4),
                    "ai_tx_loss_mwh": round(ai_tx_loss, 4),
                    "ai_net_mwh": round(ai_net, 4),
                    "approved_supply_net_mwh": round(supply_net, 4),
                    "difference_mwh": round(diff, 4),
                    "slot_approved": slot_approved,
                }
            )

        return Response(
            {
                "date": req_date.isoformat(),
                "plant_id": plant.id,
                "plant_name": plant.name,
                "approved": approved,
                "shutdown": shutdown,
                "loss_pct": loss_pct,
                "slots": slots_out,
            },
            status=status.HTTP_200_OK,
        )

class ConsumerManagerApproveGeneratorScheduleView(APIView):
    """
    Consumer manager approves demand for Generator dashboard visibility.
    Only when this is done can the Generator see demand for this consumer+date.
    """

    permission_classes = [IsAuthenticated, IsConsumerManager]

    def _validate_date_range(self, value: datetime.date) -> datetime.date:
        today = timezone.localdate()
        tomorrow = today + datetime.timedelta(days=1)
        max_date = tomorrow + datetime.timedelta(days=6)
        if value < tomorrow:
            raise ValueError('Approval is only allowed from tomorrow onwards.')
        if value > max_date:
            raise ValueError('You can only approve for the next 7 days.')
        return value

    @transaction.atomic
    def post(self, request):
        date_str = request.data.get('date')
        if not date_str:
            return Response({'detail': 'date is required (YYYY-MM-DD).'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            req_date = datetime.date.fromisoformat(date_str)
            self._validate_date_range(req_date)
        except ValueError:
            return Response({'detail': 'Invalid date format. Use YYYY-MM-DD.'}, status=status.HTTP_400_BAD_REQUEST)

        consumer = getattr(request.user, 'managed_consumer', None)
        if consumer is None:
            return Response({'detail': 'Consumer not linked for this manager user.'}, status=status.HTTP_400_BAD_REQUEST)

        approval, _created = GeneratorScheduleApproval.objects.update_or_create(
            consumer=consumer,
            date=req_date,
            defaults={'approved_by_user': request.user},
        )

        return Response(
            {
                'date': req_date,
                'generator_schedule_approved': True,
                'generator_schedule_approved_at': approval.approved_at,
            },
            status=status.HTTP_200_OK,
        )


class ConsumerManagerPlantDemandSlotsView(APIView):
    """
    Returns 96 slots for a plant's demand on a date.
    Each slot shows demand with transmission loss applied (gross).
    """

    permission_classes = [IsAuthenticated, IsConsumerManager]

    def _validate_date_range(self, value: datetime.date) -> datetime.date:
        today = timezone.localdate()
        tomorrow = today + datetime.timedelta(days=1)
        max_date = tomorrow + datetime.timedelta(days=6)
        if value < tomorrow:
            raise ValueError('Demand entry is only allowed from tomorrow onwards.')
        if value > max_date:
            raise ValueError('You can only view for the next 7 days.')
        return value

    def get(self, request):
        date_str = request.query_params.get('date')
        plant_id = request.query_params.get('plant_id')
        if not date_str or not plant_id:
            return Response(
                {'detail': 'date and plant_id query parameters are required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            req_date = datetime.date.fromisoformat(date_str)
            self._validate_date_range(req_date)
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        consumer = getattr(request.user, 'managed_consumer', None)
        if consumer is None:
            return Response({'detail': 'Consumer not linked for this manager user.'}, status=status.HTTP_400_BAD_REQUEST)

        plant = get_object_or_404(Plant, id=plant_id, consumer=consumer)
        schedule = DemandSchedule.objects.filter(plant=plant, date=req_date).first()

        from core.api.serializers import get_or_none_transmission_loss, get_transmission_loss_percent, net_to_gross

        year = req_date.year
        loss_pct = get_transmission_loss_percent(plant, year)
        tpl = get_or_none_transmission_loss(plant, year)
        state_loss_pct = float(tpl.state_transition_loss_percent) if tpl else 0
        central_loss_pct = float(tpl.central_transmission_loss_percent) if tpl else 0
        slot_map_net = {}
        slot_map_gross = {}
        if schedule and not schedule.shutdown:
            for s in schedule.slots.all():
                net_val = float(s.demand_mw or 0)
                gross_val = net_to_gross(Decimal(str(net_val)), loss_pct)
                slot_map_net[int(s.slot_index)] = net_val
                slot_map_gross[int(s.slot_index)] = float(gross_val)

        day_slots = generate_day_slots()
        slots = []
        for slot in day_slots:
            idx = slot['slot_index']
            demand_net = slot_map_net.get(idx, 0)
            demand_gross = slot_map_gross.get(idx, 0)
            slots.append({
                'slot_index': idx,
                'time_block': slot['time_block'],
                'demand_mwh_without_loss': round(demand_net, 4),
                'demand_mwh': round(demand_gross, 4),
            })

        total_net = sum(s['demand_mwh_without_loss'] for s in slots)
        total_gross = sum(s['demand_mwh'] for s in slots)
        return Response(
            {
                'date': req_date.isoformat(),
                'plant_id': plant.id,
                'plant_name': plant.name,
                'transmission_loss_percent': float(loss_pct),
                'state_transmission_loss_percent': state_loss_pct,
                'central_transmission_loss_percent': central_loss_pct,
                'shutdown': bool(schedule and schedule.shutdown) if schedule else False,
                'slots': slots,
                'total_demand_mwh_without_loss': round(total_net, 4),
                'total_demand_mwh': round(total_gross, 4),
            },
            status=status.HTTP_200_OK,
        )


class ConsumerManagerOverallDemandSlotsView(APIView):
    """
    Returns 96 slots with aggregated demand (with loss) across all plants for a date.
    Columns: time_block, no_of_plants, demand_with_loss_mwh.
    """

    permission_classes = [IsAuthenticated, IsConsumerManager]

    def _validate_date_range(self, value: datetime.date) -> datetime.date:
        today = timezone.localdate()
        tomorrow = today + datetime.timedelta(days=1)
        max_date = tomorrow + datetime.timedelta(days=6)
        if value < tomorrow:
            raise ValueError('Demand entry is only allowed from tomorrow onwards.')
        if value > max_date:
            raise ValueError('You can only view for the next 7 days.')
        return value

    def get(self, request):
        date_str = request.query_params.get('date')
        if not date_str:
            return Response(
                {'detail': 'date query parameter is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            req_date = datetime.date.fromisoformat(date_str)
            self._validate_date_range(req_date)
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        consumer = getattr(request.user, 'managed_consumer', None)
        if consumer is None:
            return Response({'detail': 'Consumer not linked for this manager user.'}, status=status.HTTP_400_BAD_REQUEST)

        from core.api.serializers import get_or_none_transmission_loss, get_transmission_loss_percent, net_to_gross

        plants = Plant.objects.filter(consumer=consumer).order_by('id')
        year = req_date.year
        slot_totals = {}  # slot_index -> demand_with_loss sum
        plants_with_schedule = 0

        for plant in plants:
            schedule = DemandSchedule.objects.filter(plant=plant, date=req_date).first()
            if not schedule or schedule.shutdown:
                continue
            plants_with_schedule += 1
            loss_pct = get_transmission_loss_percent(plant, year)
            for s in schedule.slots.all():
                net_val = float(s.demand_mw or 0)
                gross_val = net_to_gross(Decimal(str(net_val)), loss_pct)
                idx = int(s.slot_index)
                slot_totals[idx] = slot_totals.get(idx, 0) + float(gross_val)

        day_slots = generate_day_slots()
        slots = []
        for slot in day_slots:
            idx = slot['slot_index']
            slots.append({
                'slot_index': idx,
                'time_block': slot['time_block'],
                'no_of_plants': plants_with_schedule,
                'demand_with_loss_mwh': round(slot_totals.get(idx, 0), 4),
            })

        return Response(
            {
                'date': req_date.isoformat(),
                'slots': slots,
                'no_of_plants': plants_with_schedule,
            },
            status=status.HTTP_200_OK,
        )


class ConsumerManagerApproveDemandView(APIView):
    """
    Approves the whole consumer demand schedule for a specific date (tomorrow..tomorrow+6).
    This is consumer-wide (not per-plant).
    """

    permission_classes = [IsAuthenticated, IsConsumerManager]

    def _validate_date_range(self, value: datetime.date) -> datetime.date:
        today = timezone.localdate()
        tomorrow = today + datetime.timedelta(days=1)
        max_date = tomorrow + datetime.timedelta(days=6)
        if value < tomorrow:
            raise ValueError('Demand approval is only allowed from tomorrow onwards.')
        if value > max_date:
            raise ValueError('You can only approve for the next 7 days.')
        return value

    @transaction.atomic
    def post(self, request):
        date_str = request.data.get('date')
        if not date_str:
            return Response({'detail': 'date is required (YYYY-MM-DD).'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            req_date = datetime.date.fromisoformat(date_str)
            self._validate_date_range(req_date)
        except ValueError:
            return Response({'detail': 'Invalid date format. Use YYYY-MM-DD.'}, status=status.HTTP_400_BAD_REQUEST)

        consumer = getattr(request.user, 'managed_consumer', None)
        if consumer is None:
            return Response({'detail': 'Consumer not linked for this manager user.'}, status=status.HTTP_400_BAD_REQUEST)

        approval, _created = ConsumerDemandApproval.objects.update_or_create(
            consumer=consumer,
            date=req_date,
            defaults={'approved_by_user': request.user},
        )

        return Response(
            {'date': req_date, 'approved': True, 'approved_at': approval.approved_at},
            status=status.HTTP_200_OK,
        )


class ConsumerManagerDemandWeekView(APIView):
    """
    Returns daily total demand and supply for tomorrow..tomorrow+6.
    Used by the consumer manager dashboard graphs.
    """

    permission_classes = [IsAuthenticated, IsConsumerManager]

    def get(self, request):
        consumer = getattr(request.user, 'managed_consumer', None)
        if consumer is None:
            return Response({'detail': 'Consumer not linked for this manager user.'}, status=status.HTTP_400_BAD_REQUEST)

        today = timezone.localdate()
        tomorrow = today + datetime.timedelta(days=1)
        dates = [tomorrow + datetime.timedelta(days=i) for i in range(7)]

        # Fetch demand schedules.
        schedules = (
            DemandSchedule.objects.filter(plant__consumer=consumer, date__in=dates)
            .prefetch_related('slots')
        )
        schedule_by_date = {}
        for s in schedules:
            schedule_by_date.setdefault(s.date, []).append(s)

        # Fetch supply schedules (generator-submitted) for same dates.
        supply_schedules = (
            GeneratorSupplySchedule.objects.filter(consumer=consumer, date__in=dates)
            .prefetch_related('slots')
        )
        supply_by_date = {s.date: sum((slot.supply_mwh for slot in s.slots.all()), Decimal('0')) for s in supply_schedules}

        from core.api.serializers import get_transmission_loss_percent, net_to_gross

        series = []
        for d in dates:
            day_schedules = schedule_by_date.get(d, [])
            shutdown = False
            total_demand_gross = Decimal('0')
            for sched in day_schedules:
                if sched.shutdown:
                    shutdown = True
                if not sched.shutdown:
                    plant_total_net = sum((slot.demand_mw for slot in sched.slots.all()), Decimal('0'))
                    loss_pct = get_transmission_loss_percent(sched.plant, d.year)
                    total_demand_gross += net_to_gross(plant_total_net, loss_pct)

            total_supply = supply_by_date.get(d, Decimal('0'))
            if isinstance(total_supply, Decimal):
                total_supply = float(total_supply)

            series.append(
                {
                    'date': d.isoformat(),
                    'date_label': d.strftime('%a %d %b'),
                    'total_demand_mwh': str(total_demand_gross),
                    'total_supply_mwh': str(total_supply),
                    'shutdown': shutdown,
                }
            )

        return Response({'series': series}, status=status.HTTP_200_OK)


class ConsumerManagerDemandDataView(APIView):
    """
    Slot-wise (96 × 15-min) demand and generator supply for a single day.
    GET /api/demand-data/?date=YYYY-MM-DD
    """

    permission_classes = [IsAuthenticated, IsConsumerManager]

    def _validate_date_range(self, value: datetime.date) -> datetime.date:
        today = timezone.localdate()
        yesterday = today - datetime.timedelta(days=1)
        tomorrow = today + datetime.timedelta(days=1)
        max_date = tomorrow + datetime.timedelta(days=6)
        if value < yesterday:
            raise ValueError('Demand data is only available from yesterday onwards.')
        if value > max_date:
            raise ValueError('You can only view data through the 7-day window (tomorrow + 6 days).')
        return value

    def get(self, request):
        date_str = request.query_params.get('date')
        if not date_str:
            return Response({'detail': 'date query parameter is required.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            req_date = datetime.date.fromisoformat(date_str)
            self._validate_date_range(req_date)
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        consumer = getattr(request.user, 'managed_consumer', None)
        if consumer is None:
            return Response({'detail': 'Consumer not linked for this manager user.'}, status=status.HTTP_400_BAD_REQUEST)

        from core.api.serializers import get_transmission_loss_percent, net_to_gross

        plants = Plant.objects.filter(consumer=consumer).order_by('id')
        year = req_date.year
        slot_totals: dict[int, float] = {}
        for plant in plants:
            schedule = DemandSchedule.objects.filter(plant=plant, date=req_date).first()
            if not schedule or schedule.shutdown:
                continue
            loss_pct = get_transmission_loss_percent(plant, year)
            for s in schedule.slots.all():
                net_val = float(s.demand_mw or 0)
                gross_val = float(net_to_gross(Decimal(str(net_val)), loss_pct))
                idx = int(s.slot_index)
                slot_totals[idx] = slot_totals.get(idx, 0.0) + gross_val

        demand_values = [round(slot_totals.get(i, 0.0), 4) for i in range(1, SLOTS_PER_DAY + 1)]

        supply_by_slot: dict[int, float] = {}
        gss = GeneratorSupplySchedule.objects.filter(consumer=consumer, date=req_date).first()
        if gss:
            for s in gss.slots.all():
                supply_by_slot[int(s.slot_index)] = float(s.supply_mwh or 0)
        supply_values = [round(supply_by_slot.get(i, 0.0), 4) for i in range(1, SLOTS_PER_DAY + 1)]

        labels = []
        for i in range(SLOTS_PER_DAY):
            hour = i // 4
            minute = (i % 4) * 15
            labels.append(f'{hour}:{minute:02d}')

        return Response(
            {
                'date': req_date.isoformat(),
                'labels': labels,
                'values': demand_values,
                'supply_values': supply_values,
            },
            status=status.HTTP_200_OK,
        )


def energy_manager_display_name(consumer: Consumer) -> str:
    cm = getattr(consumer, 'consumer_manager', None)
    if not cm:
        return ''
    return (cm.name or '').strip() or cm.email


class GeneratorConsumerManagersView(APIView):
    """
    Lists consumer managers for Generator (supply / dashboard / allocation list / reports).
    Requires date query param (any calendar date; used to filter optional allocation_approved_only).
    By default returns all consumers with a consumer manager.
    Optional: allocation_approved_only=1 — only consumers whose overall allocation is APPROVED for that date
    (used by Generator Allocation consumer list).
    """

    permission_classes = [IsAuthenticated, IsGenerator]

    def get(self, request):
        date_str = request.query_params.get('date')
        if not date_str:
            return Response({'detail': 'date query parameter is required (YYYY-MM-DD).'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            req_date = datetime.date.fromisoformat(date_str)
        except ValueError:
            return Response({'detail': 'Invalid date format. Use YYYY-MM-DD.'}, status=status.HTTP_400_BAD_REQUEST)

        approved_only = request.query_params.get('allocation_approved_only', '').lower() in ('1', 'true', 'yes')

        qs = (
            Consumer.objects.select_related('consumer_manager')
            .filter(consumer_manager__isnull=False)
            .exclude(name__iexact='Consumer2')
            .order_by('id')
        )
        seen_managers: set[int] = set()
        results = []
        for c in qs:
            cm = c.consumer_manager
            if not cm or cm.id in seen_managers:
                continue
            if approved_only:
                run = ConsumerGeneratorAllocationRun.objects.filter(consumer=c, date=req_date).first()
                if not run or run.status != ConsumerGeneratorAllocationRun.Status.APPROVED:
                    continue
            seen_managers.add(cm.id)
            results.append(
                {
                    'consumer_id': c.id,
                    'consumer_manager_user_id': cm.id,
                    'consumer_manager_email': cm.email,
                    'consumer_name': c.name,
                    'energy_manager_name': energy_manager_display_name(c),
                }
            )
        return Response({'results': results}, status=status.HTTP_200_OK)


class GeneratorConsumerDemandSlotsView(APIView):
    """
    Returns the total demand schedule across all plants of the given consumer manager.
    Response: 96 slots (15-min interval for full day).
    """

    permission_classes = [IsAuthenticated, IsGenerator]

    def _validate_date_range(self, value: datetime.date) -> datetime.date:
        today = timezone.localdate()
        tomorrow = today + datetime.timedelta(days=1)
        max_date = tomorrow + datetime.timedelta(days=6)
        if value < tomorrow:
            raise ValueError('Demand is only available from tomorrow onwards.')
        if value > max_date:
            raise ValueError('You can only view the next 7 days.')
        return value

    def get(self, request):
        consumer_manager_user_id = request.query_params.get('consumer_manager_user_id')
        date_str = request.query_params.get('date')
        if not consumer_manager_user_id or not date_str:
            return Response({'detail': 'consumer_manager_user_id and date are required.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            req_date = datetime.date.fromisoformat(date_str)
            self._validate_date_range(req_date)
        except ValueError:
            return Response({'detail': 'Invalid date format. Use YYYY-MM-DD.'}, status=status.HTTP_400_BAD_REQUEST)

        consumer = get_object_or_404(Consumer, consumer_manager_id=consumer_manager_user_id)

        if not GeneratorScheduleApproval.objects.filter(consumer=consumer, date=req_date).exists():
            return Response(
                {'detail': 'Demand for this consumer is not approved for Generator visibility for this date.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        slot_totals = aggregate_consumer_demand_mwh_by_slot(consumer, req_date)

        day_slots = generate_day_slots()
        resp_slots = []
        for slot in day_slots:
            idx = slot['slot_index']
            resp_slots.append(
                {
                    'slot_index': idx,
                    'slot_time': slot['slot_time'].strftime('%H:%M'),
                    'time_block': slot['time_block'],
                    'demand_mwh': str(slot_totals.get(idx, Decimal('0')) or 0),
                }
            )

        return Response(
            {
                'consumer_manager_user_id': int(consumer_manager_user_id),
                'consumer_name': consumer.name,
                'date': req_date,
                'slots': resp_slots,
            },
            status=status.HTTP_200_OK,
        )


class GeneratorDummySupplySlotsView(APIView):
    permission_classes = [IsAuthenticated, IsGenerator]

    def _validate_date_range(self, value: datetime.date) -> datetime.date:
        today = timezone.localdate()
        max_date = today + datetime.timedelta(days=7)
        if value < today:
            raise ValueError('Supply is only available from today onwards.')
        if value > max_date:
            raise ValueError('You can only fetch supply for today and the next 7 calendar days (8 days total).')
        return value

    def get(self, request):
        consumer_manager_user_id = request.query_params.get('consumer_manager_user_id')
        date_str = request.query_params.get('date')
        if not consumer_manager_user_id or not date_str:
            return Response({'detail': 'consumer_manager_user_id and date are required.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            req_date = datetime.date.fromisoformat(date_str)
        except ValueError:
            return Response({'detail': 'Invalid date format. Use YYYY-MM-DD.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            req_date = self._validate_date_range(req_date)
        except ValueError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)

        consumer = get_object_or_404(Consumer, consumer_manager_id=consumer_manager_user_id)

        day_seed = (req_date.toordinal() % 11) + 1
        manager_seed = (int(consumer_manager_user_id) % 7) + 1
        base_val = Decimal('8.0') + Decimal(str((day_seed + manager_seed) / 10))

        resp_slots = []
        for slot in generate_day_slots():
            idx = int(slot['slot_index'])
            hour = (idx - 1) // 4
            if 8 <= hour <= 19:
                band = Decimal('3.0')
            elif 6 <= hour < 8 or 20 <= hour <= 22:
                band = Decimal('1.5')
            else:
                band = Decimal('0.6')
            zigzag = Decimal(str((idx % 4) * 0.2))
            raw_value = (base_val + band + zigzag).quantize(Decimal('0.01'))

            resp_slots.append(
                {
                    'slot_index': idx,
                    'slot_time': slot['slot_time'].strftime('%H:%M'),
                    'time_block': slot['time_block'],
                    'supply_mwh': str(raw_value),
                }
            )

        return Response(
            {
                'consumer_manager_user_id': int(consumer_manager_user_id),
                'consumer_name': consumer.name,
                'date': req_date,
                'source': 'dummy',
                'slots': resp_slots,
            },
            status=status.HTTP_200_OK,
        )


def _slot_approval_index_set(consumer, req_date: datetime.date):
    """0-based slot_index values that have at least one slot approval row (consumer+date)."""
    return {int(s) for s in SlotAllocationApproval.objects.filter(consumer=consumer, date=req_date).values_list('slot_index', flat=True)}


def _generator_day_needs_allocation_context(consumer, req_date: datetime.date) -> bool:
    """If False, per-day helpers can skip load_consumer_allocation_slot_context (savings analysis only)."""
    run = ConsumerGeneratorAllocationRun.objects.filter(consumer=consumer, date=req_date).first()
    if run and run.status == ConsumerGeneratorAllocationRun.Status.APPROVED:
        return True
    return bool(_slot_approval_index_set(consumer, req_date))


def generator_approved_allocation_day_totals(consumer, req_date, generator_user=None, *, allocation_context=None):
    """
    Same daily totals as GeneratorApprovedAllocationSlotsView: sum of per-slot total_mwh (approved plant gross)
    and sum of per-slot generator supply. If generator_user is set, only that user's schedule is used; otherwise
    the single consumer+date schedule (unique) is used (e.g. plant user dashboard).
    Returns (accounted_mwh, total_generator_supply_mwh, total_iex_mwh).

    When the overall day run is not APPROVED, this still:
      - always surfaces submitted generator supply for the day;
      - for slots with consumer-manager slot-level approvals, uses the same base+AI + IEX accounting;
      - for slots with no slot approval, leaves accounted/IEX for that slot at 0 (does not mark full supply as IEX).
    """
    run = ConsumerGeneratorAllocationRun.objects.filter(consumer=consumer, date=req_date).first()
    day_approved = run and run.status == ConsumerGeneratorAllocationRun.Status.APPROVED
    slots_with_approval = _slot_approval_index_set(consumer, req_date)

    qs = GeneratorSupplySchedule.objects.filter(consumer=consumer, date=req_date)
    if generator_user is not None:
        qs = qs.filter(submitted_by_user=generator_user)
    schedule = qs.first()
    supply_by_slot: dict[int, Decimal] = {}
    if schedule:
        for s in schedule.slots.all():
            supply_by_slot[int(s.slot_index)] = Decimal(str(s.supply_mwh or 0))
    day_slots = generate_day_slots()
    total_supply = sum((supply_by_slot.get(int(s['slot_index']), Decimal('0')) for s in day_slots), start=Decimal('0'))

    if not day_approved and not slots_with_approval:
        return Decimal('0'), total_supply, Decimal('0')

    if allocation_context is not None:
        ctx = allocation_context
    else:
        ctx = load_consumer_allocation_slot_context(consumer, req_date)
    plants = ctx.get('plants') or []
    plants_out = [
        {
            'plant_id': int(p.get('plant_id') or 0),
            'plant_name': p.get('plant_name') or '',
            'allocated_total_mwh': str(p.get('allocated_total_mwh') or 0),
        }
        for p in plants
        if int(p.get('plant_id') or 0) > 0
    ]

    base_gross_per_slot_by_plant: dict[int, Decimal] = {}
    for p in plants_out:
        pid = int(p['plant_id'])
        total_gross = Decimal(str(p.get('allocated_total_mwh') or 0))
        base_gross_per_slot_by_plant[pid] = total_gross / Decimal(str(SLOTS_PER_DAY))

    approvals = (
        SlotAllocationApproval.objects.filter(consumer=consumer, date=req_date, plant_id__isnull=False)
        .values('slot_index', 'plant_id')
        .annotate(total=Sum('allocated_mwh'))
    )
    approved_ai_by_slot_plant: dict[int, dict[int, Decimal]] = {}
    for r in approvals:
        idx = int(r['slot_index'])
        pid = int(r['plant_id'])
        approved_ai_by_slot_plant.setdefault(idx, {})[pid] = Decimal(str(r['total'] or 0))

    total_alloc = Decimal('0')
    total_iex = Decimal('0')
    for slot in day_slots:
        idx = int(slot['slot_index'])
        if not day_approved and idx not in slots_with_approval:
            continue
        total_slot = Decimal('0')
        for p in plants_out:
            pid = int(p['plant_id'])
            base_val = base_gross_per_slot_by_plant.get(pid, Decimal('0'))
            ai_val = (approved_ai_by_slot_plant.get(idx, {}) or {}).get(pid, Decimal('0'))
            total_slot += base_val + ai_val
        gen_s = supply_by_slot.get(idx, Decimal('0'))
        iex_slot = max(Decimal('0'), gen_s - total_slot)
        total_iex += iex_slot
        total_alloc += total_slot + iex_slot

    return total_alloc, total_supply, total_iex


def generator_day_slot_supply_and_accounted(consumer, req_date, generator_user=None, *, allocation_context=None):
    """
    Per-slot (96) generator supply and accounted MWh (plants + IEX) for one consumer/day.
    If generator_user is set, only that user's schedule is used; otherwise the consumer+date schedule.
    Returns ([96 supply], [96 accounted]). Supply is always from the schedule when present.
    Accounted is 0 for a slot unless the day is fully approved or that slot has slot-level approval.
    """
    run = ConsumerGeneratorAllocationRun.objects.filter(consumer=consumer, date=req_date).first()
    day_approved = run and run.status == ConsumerGeneratorAllocationRun.Status.APPROVED
    slots_with_approval = _slot_approval_index_set(consumer, req_date)

    qs = GeneratorSupplySchedule.objects.filter(consumer=consumer, date=req_date)
    if generator_user is not None:
        qs = qs.filter(submitted_by_user=generator_user)
    schedule = qs.first()
    supply_by_slot: dict[int, Decimal] = {}
    if schedule:
        for s in schedule.slots.all():
            supply_by_slot[int(s.slot_index)] = Decimal(str(s.supply_mwh or 0))

    if not day_approved and not slots_with_approval:
        supply_list = [supply_by_slot.get(i, Decimal('0')) for i in range(1, int(SLOTS_PER_DAY) + 1)]
        return supply_list, [Decimal('0')] * int(SLOTS_PER_DAY)

    if allocation_context is not None:
        ctx = allocation_context
    else:
        ctx = load_consumer_allocation_slot_context(consumer, req_date)
    plants = ctx.get('plants') or []
    plants_out = [
        {
            'plant_id': int(p.get('plant_id') or 0),
            'plant_name': p.get('plant_name') or '',
            'allocated_total_mwh': str(p.get('allocated_total_mwh') or 0),
        }
        for p in plants
        if int(p.get('plant_id') or 0) > 0
    ]

    base_gross_per_slot_by_plant: dict[int, Decimal] = {}
    for p in plants_out:
        pid = int(p['plant_id'])
        total_gross = Decimal(str(p.get('allocated_total_mwh') or 0))
        base_gross_per_slot_by_plant[pid] = total_gross / Decimal(str(SLOTS_PER_DAY))

    approvals = (
        SlotAllocationApproval.objects.filter(consumer=consumer, date=req_date, plant_id__isnull=False)
        .values('slot_index', 'plant_id')
        .annotate(total=Sum('allocated_mwh'))
    )
    approved_ai_by_slot_plant: dict[int, dict[int, Decimal]] = {}
    for r in approvals:
        idx = int(r['slot_index'])
        pid = int(r['plant_id'])
        approved_ai_by_slot_plant.setdefault(idx, {})[pid] = Decimal(str(r['total'] or 0))

    day_slots = generate_day_slots()
    supply_list: list[Decimal] = []
    accounted_list: list[Decimal] = []
    for slot in day_slots:
        idx = int(slot['slot_index'])
        gen_s = supply_by_slot.get(idx, Decimal('0'))
        supply_list.append(gen_s)
        if not day_approved and idx not in slots_with_approval:
            accounted_list.append(Decimal('0'))
            continue
        total_slot = Decimal('0')
        for p in plants_out:
            pid = int(p['plant_id'])
            base_val = base_gross_per_slot_by_plant.get(pid, Decimal('0'))
            ai_val = (approved_ai_by_slot_plant.get(idx, {}) or {}).get(pid, Decimal('0'))
            total_slot += base_val + ai_val
        iex_slot = max(Decimal('0'), gen_s - total_slot)
        accounted_list.append(total_slot + iex_slot)

    return supply_list, accounted_list


def _slots_to_hourly(supply_96, accounted_96):
    """Aggregate 96 x 15-min slots into 24 hourly MWh totals."""
    sh: list[float] = []
    ah: list[float] = []
    for h in range(24):
        lo = h * 4
        hi = lo + 4
        sh.append(float(round(sum(supply_96[lo:hi], start=Decimal('0')), 4)))
        ah.append(float(round(sum(accounted_96[lo:hi], start=Decimal('0')), 4)))
    return sh, ah


def generator_plant_approved_gross_slots_mwh(consumer, req_date, plant_id: int, *, allocation_context=None) -> list[Decimal]:
    """
    Per 15-min slot approved gross MWh (base + slot AI) for one plant; 96 values.
    If the day run is fully APPROVED, every slot includes base. Otherwise only slots with
    slot-level approval include base+AI; other slots are 0.
    """
    run = ConsumerGeneratorAllocationRun.objects.filter(consumer=consumer, date=req_date).first()
    day_approved = run and run.status == ConsumerGeneratorAllocationRun.Status.APPROVED
    slots_with_approval = _slot_approval_index_set(consumer, req_date)
    if plant_id <= 0:
        return [Decimal('0')] * int(SLOTS_PER_DAY)
    if not day_approved and not slots_with_approval:
        return [Decimal('0')] * int(SLOTS_PER_DAY)

    if allocation_context is not None:
        ctx = allocation_context
    else:
        ctx = load_consumer_allocation_slot_context(consumer, req_date)
    plants = ctx.get('plants') or []
    plants_out = [
        {
            'plant_id': int(p.get('plant_id') or 0),
            'allocated_total_mwh': str(p.get('allocated_total_mwh') or 0),
        }
        for p in plants
        if int(p.get('plant_id') or 0) > 0
    ]
    base_gross_per_slot_by_plant: dict[int, Decimal] = {}
    for p in plants_out:
        pid = int(p['plant_id'])
        total_gross = Decimal(str(p.get('allocated_total_mwh') or 0))
        base_gross_per_slot_by_plant[pid] = total_gross / Decimal(str(SLOTS_PER_DAY))

    approvals = (
        SlotAllocationApproval.objects.filter(
            consumer=consumer, date=req_date, plant_id=plant_id
        )
        .values('slot_index')
        .annotate(total=Sum('allocated_mwh'))
    )
    ai_by_slot = {int(r['slot_index']): Decimal(str(r['total'] or 0)) for r in approvals}

    out: list[Decimal] = []
    for slot in generate_day_slots():
        idx = int(slot['slot_index'])
        if not day_approved and idx not in slots_with_approval:
            out.append(Decimal('0'))
            continue
        base_val = base_gross_per_slot_by_plant.get(plant_id, Decimal('0'))
        ai_val = ai_by_slot.get(idx, Decimal('0'))
        out.append(base_val + ai_val)
    return out


def generator_plant_approved_gross_mwh(consumer, req_date, plant_id: int) -> Decimal:
    """Total approved gross MWh (base + slot AI) for one plant for one day. 0 if not approved."""
    return sum(generator_plant_approved_gross_slots_mwh(consumer, req_date, plant_id), start=Decimal('0'))


class GeneratorApprovedAllocationSlotsView(APIView):
    """
    Returns Consumer Manager overall-approved allocation (gross, with transmission loss) for a day.
    - Values are per-plant per-slot: base_gross_per_slot + approved_ai_gross_per_slot
    - Only available once Consumer Manager approves the overall allocation run.
    """

    permission_classes = [IsAuthenticated, IsGenerator]

    def get(self, request):
        consumer_manager_user_id = request.query_params.get('consumer_manager_user_id')
        date_str = request.query_params.get('date')
        if not consumer_manager_user_id or not date_str:
            return Response({'detail': 'consumer_manager_user_id and date are required.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            req_date = datetime.date.fromisoformat(date_str)
        except ValueError:
            return Response({'detail': 'Invalid date format. Use YYYY-MM-DD.'}, status=status.HTTP_400_BAD_REQUEST)

        consumer = get_object_or_404(Consumer, consumer_manager_id=consumer_manager_user_id)
        run = ConsumerGeneratorAllocationRun.objects.filter(consumer=consumer, date=req_date).first()
        if not run or run.status != ConsumerGeneratorAllocationRun.Status.APPROVED:
            return Response(
                {'detail': 'Allocation for this consumer is not approved by Consumer Manager for this date.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Generator's own supply schedule for this consumer+day (slot-wise).
        schedule = GeneratorSupplySchedule.objects.filter(consumer=consumer, date=req_date, submitted_by_user=request.user).first()
        supply_by_slot: dict[int, Decimal] = {}
        if schedule:
            for s in schedule.slots.all():
                supply_by_slot[int(s.slot_index)] = Decimal(str(s.supply_mwh or 0))

        ctx = load_consumer_allocation_slot_context(consumer, req_date)
        plants = ctx.get('plants') or []
        plants_out = [
            {
                'plant_id': int(p.get('plant_id') or 0),
                'plant_name': p.get('plant_name') or '',
                'allocated_total_mwh': str(p.get('allocated_total_mwh') or 0),
            }
            for p in plants
            if int(p.get('plant_id') or 0) > 0
        ]

        base_gross_per_slot_by_plant: dict[int, Decimal] = {}
        for p in plants_out:
            pid = int(p['plant_id'])
            total_gross = Decimal(str(p.get('allocated_total_mwh') or 0))
            base_gross_per_slot_by_plant[pid] = total_gross / Decimal(str(SLOTS_PER_DAY))

        approvals = (
            SlotAllocationApproval.objects.filter(consumer=consumer, date=req_date, plant_id__isnull=False)
            .values('slot_index', 'plant_id')
            .annotate(total=Sum('allocated_mwh'))
        )
        approved_ai_by_slot_plant: dict[int, dict[int, Decimal]] = {}
        for r in approvals:
            idx = int(r['slot_index'])
            pid = int(r['plant_id'])
            approved_ai_by_slot_plant.setdefault(idx, {})[pid] = Decimal(str(r['total'] or 0))

        day_slots = generate_day_slots()
        slots_out = []
        for slot in day_slots:
            idx = int(slot['slot_index'])
            per_plant = []
            total_slot = Decimal('0')
            gen_supply = supply_by_slot.get(idx, Decimal('0'))
            for p in plants_out:
                pid = int(p['plant_id'])
                base_val = base_gross_per_slot_by_plant.get(pid, Decimal('0'))
                ai_val = (approved_ai_by_slot_plant.get(idx, {}) or {}).get(pid, Decimal('0'))
                val = base_val + ai_val
                total_slot += val
                per_plant.append({'plant_id': pid, 'mwh': str(val)})
            iex_mwh = max(Decimal('0'), gen_supply - total_slot)
            accounted = total_slot + iex_mwh
            slots_out.append(
                {
                    'slot_index': idx,
                    'slot_time': slot['slot_time'].strftime('%H:%M'),
                    'time_block': slot['time_block'],
                    'generator_supply_mwh': str(gen_supply),
                    'allocations': per_plant,
                    'total_mwh': str(total_slot),
                    'iex_mwh': str(iex_mwh),
                    'total_accounted_mwh': str(accounted),
                    'difference_mwh': '0',
                }
            )

        return Response(
            {
                'consumer_manager_user_id': int(consumer_manager_user_id),
                'consumer_name': consumer.name,
                'date': req_date,
                'plants': plants_out,
                'slots': slots_out,
            },
            status=status.HTTP_200_OK,
        )


class PlantApprovedAllocationSlotsView(APIView):
    """
    Approved allocation and demand entry per 15-min slot for the authenticated plant user only:
    one plant in `plants`, slot rows scoped to that plant (no other plants, no consumer-wide totals).
    """

    permission_classes = [IsAuthenticated, IsPlantUser]

    def get(self, request):
        date_str = request.query_params.get('date')
        if not date_str:
            return Response({'detail': 'date query parameter is required.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            req_date = datetime.date.fromisoformat(date_str)
        except ValueError:
            return Response({'detail': 'Invalid date format. Use YYYY-MM-DD.'}, status=status.HTTP_400_BAD_REQUEST)

        plant = get_plant_for_plant_user(request.user)
        consumer = plant.consumer
        cm = consumer.consumer_manager
        if not cm:
            return Response({'detail': 'Consumer has no manager linked.'}, status=status.HTTP_400_BAD_REQUEST)

        run = ConsumerGeneratorAllocationRun.objects.filter(consumer=consumer, date=req_date).first()
        if not run or run.status != ConsumerGeneratorAllocationRun.Status.APPROVED:
            return Response(
                {'detail': 'Allocation for this consumer is not approved for this date.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        ctx = load_consumer_allocation_slot_context(consumer, req_date)
        plants = ctx.get('plants') or []
        plants_out = [
            {
                'plant_id': int(p.get('plant_id') or 0),
                'plant_name': p.get('plant_name') or '',
                'allocated_total_mwh': str(p.get('allocated_total_mwh') or 0),
            }
            for p in plants
            if int(p.get('plant_id') or 0) > 0
        ]
        plants_out = [p for p in plants_out if int(p['plant_id']) == int(plant.id)]
        if not plants_out:
            plants_out = [
                {
                    'plant_id': int(plant.id),
                    'plant_name': plant.name or '',
                    'allocated_total_mwh': '0',
                }
            ]

        base_gross_per_slot_by_plant: dict[int, Decimal] = {}
        for p in plants_out:
            pid = int(p['plant_id'])
            total_gross = Decimal(str(p.get('allocated_total_mwh') or 0))
            base_gross_per_slot_by_plant[pid] = total_gross / Decimal(str(SLOTS_PER_DAY))

        approvals = (
            SlotAllocationApproval.objects.filter(consumer=consumer, date=req_date, plant_id__isnull=False)
            .values('slot_index', 'plant_id')
            .annotate(total=Sum('allocated_mwh'))
        )
        approved_ai_by_slot_plant: dict[int, dict[int, Decimal]] = {}
        for r in approvals:
            idx = int(r['slot_index'])
            pid = int(r['plant_id'])
            approved_ai_by_slot_plant.setdefault(idx, {})[pid] = Decimal(str(r['total'] or 0))

        demand_gross_by_plant = _get_demand_gross_by_plant_and_slot(consumer, req_date, year=req_date.year)

        day_slots = generate_day_slots()
        slots_out = []
        for slot in day_slots:
            idx = int(slot['slot_index'])
            per_plant = []
            demand_per_plant = []
            total_slot = Decimal('0')
            total_dem_slot = Decimal('0')
            for p in plants_out:
                pid = int(p['plant_id'])
                base_val = base_gross_per_slot_by_plant.get(pid, Decimal('0'))
                ai_val = (approved_ai_by_slot_plant.get(idx, {}) or {}).get(pid, Decimal('0'))
                val = base_val + ai_val
                total_slot += val
                per_plant.append({'plant_id': pid, 'mwh': str(val)})
                dv = (demand_gross_by_plant.get(pid) or {}).get(idx, Decimal('0'))
                total_dem_slot += dv
                demand_per_plant.append({'plant_id': pid, 'mwh': str(dv)})
            slot_diff = total_slot - total_dem_slot
            iex_mwh = Decimal('0')
            accounted = total_slot + iex_mwh
            slots_out.append(
                {
                    'slot_index': idx,
                    'slot_time': slot['slot_time'].strftime('%H:%M'),
                    'time_block': slot['time_block'],
                    'demand_entry_plants': demand_per_plant,
                    'demand_entry_total_mwh': str(total_dem_slot),
                    'allocations': per_plant,
                    'total_mwh': str(total_slot),
                    'iex_mwh': str(iex_mwh),
                    'total_accounted_mwh': str(accounted),
                    'difference_mwh': str(slot_diff),
                }
            )

        return Response(
            {
                'consumer_manager_user_id': int(cm.id),
                'consumer_name': consumer.name,
                'date': req_date,
                'plants': plants_out,
                'slots': slots_out,
            },
            status=status.HTTP_200_OK,
        )


GENERATOR_SUPPLY_REVISION_BUFFER_SLOTS = 6


def _clamp_tz_offset_minutes(raw: object) -> int | None:
    """Browser sends -Date.getTimezoneOffset() (minutes east of UTC, e.g. 330 for India)."""
    if raw is None or raw == '':
        return None
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return None
    if v < -840 or v > 840:
        return None
    return v


def _generator_wall_clock(client_tz_offset_minutes: int | None) -> tuple[datetime.datetime, datetime.date]:
    """
    Wall-clock "now" for slot math. If client_tz_offset_minutes is set (from browser),
    use fixed offset from UTC so locks align with the user's local time. Otherwise Django's default TZ.
    """
    utc_now = timezone.now()
    if client_tz_offset_minutes is None:
        wall = timezone.localtime()
        return wall, wall.date()
    tz = datetime.timezone(datetime.timedelta(minutes=client_tz_offset_minutes))
    wall = utc_now.astimezone(tz)
    return wall, wall.date()


def generator_supply_revision_edit_bounds(
    req_date: datetime.date,
    client_tz_offset_minutes: int | None = None,
) -> tuple[int, int]:
    """
    Quarter-hour slots are 1–96. Returns (first_editable_slot, locked_through_slot).
    Slots 1..locked_through must match the saved schedule when updating after CM approval (today only).
    Uses the client's wall clock when client_tz_offset_minutes is provided (GET query / POST body).
    """
    wall, user_today = _generator_wall_clock(client_tz_offset_minutes)
    if req_date > user_today:
        return 1, 0
    if req_date < user_today:
        return 97, 96
    minutes_from_midnight = wall.hour * 60 + wall.minute + wall.second / 60.0
    current_slot = int(minutes_from_midnight // 15) + 1
    current_slot = max(1, min(96, current_slot))
    locked_through = min(96, current_slot + GENERATOR_SUPPLY_REVISION_BUFFER_SLOTS)
    return locked_through + 1, locked_through


def generator_supply_planning_date_is_user_today(
    req_date: datetime.date,
    client_tz_offset_minutes: int | None,
) -> bool:
    _, user_today = _generator_wall_clock(client_tz_offset_minutes)
    return req_date == user_today


GENERATOR_UPLOAD_REVISION_CM_WINDOW_MINUTES = 15


def expire_pending_generator_supply_upload_revisions(now=None):
    """
    Pending generator upload revisions whose deadline has passed become auto-approved (slots stay as submitted).
    """
    now = now or timezone.now()
    return GeneratorSupplyUploadRevision.objects.filter(
        cm_review_status=GeneratorSupplyUploadRevision.CMReviewStatus.PENDING,
        deadline_at__lte=now,
    ).update(
        cm_review_status=GeneratorSupplyUploadRevision.CMReviewStatus.AUTO_APPROVED,
        resolved_at=now,
        deadline_at=None,
    )


def _auto_resolve_stacked_pending_upload_revisions(schedule):
    """If a new upload revision is created while another is still pending, finalize the older one as auto-approved."""
    now = timezone.now()
    GeneratorSupplyUploadRevision.objects.filter(
        schedule=schedule,
        cm_review_status=GeneratorSupplyUploadRevision.CMReviewStatus.PENDING,
    ).update(
        cm_review_status=GeneratorSupplyUploadRevision.CMReviewStatus.AUTO_APPROVED,
        resolved_at=now,
        deadline_at=None,
    )


def _serialize_generator_upload_revision_row(ur: GeneratorSupplyUploadRevision, now: datetime.datetime) -> dict:
    deadline = ur.deadline_at
    sec_rem = 0
    if ur.cm_review_status == GeneratorSupplyUploadRevision.CMReviewStatus.PENDING and deadline:
        sec_rem = max(0, int((deadline - now).total_seconds()))
    return {
        'revision_number': ur.revision_number,
        'label': f'Revision {ur.revision_number}',
        'cm_review_status': ur.cm_review_status,
        'deadline_at': deadline.isoformat() if deadline else None,
        'resolved_at': ur.resolved_at.isoformat() if ur.resolved_at else None,
        'seconds_remaining': sec_rem,
        'changed_slots': [
            {
                'slot_index': int(d.slot_index),
                'previous_mwh': float(d.previous_mwh),
                'new_mwh': float(d.new_mwh),
            }
            for d in sorted(ur.deltas.all(), key=lambda x: x.slot_index)
        ],
    }


class ConsumerManagerGeneratorSupplyUploadRevisionsPendingView(APIView):
    """
    Lists generator supply upload revisions awaiting Consumer Manager review for the managed consumer.
    """

    permission_classes = [IsAuthenticated, IsConsumerManager]

    def _validate_view_date(self, value: datetime.date) -> datetime.date:
        today = timezone.localdate()
        if value > today:
            raise ValueError('You can only view revisions up to today.')
        return value

    def get(self, request):
        expire_pending_generator_supply_upload_revisions()
        date_str = request.query_params.get('date')
        if not date_str:
            return Response({'detail': 'date query parameter is required.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            req_date = datetime.date.fromisoformat(date_str)
            self._validate_view_date(req_date)
        except ValueError:
            return Response({'detail': 'Invalid date format. Use YYYY-MM-DD.'}, status=status.HTTP_400_BAD_REQUEST)

        consumer = getattr(request.user, 'managed_consumer', None)
        if consumer is None:
            return Response({'detail': 'Consumer not linked for this manager user.'}, status=status.HTTP_400_BAD_REQUEST)

        qs = (
            GeneratorSupplyUploadRevision.objects.filter(
                schedule__consumer=consumer,
                schedule__date=req_date,
                cm_review_status=GeneratorSupplyUploadRevision.CMReviewStatus.PENDING,
            )
            .select_related('schedule')
            .prefetch_related('deltas')
            .order_by('revision_number')
        )

        now = timezone.now()
        pending = []
        for ur in qs:
            row = _serialize_generator_upload_revision_row(ur, now)
            row['id'] = ur.id
            row['date'] = req_date.isoformat()
            pending.append(row)

        return Response({'date': req_date, 'pending': pending}, status=status.HTTP_200_OK)


class ConsumerManagerGeneratorSupplyUploadRevisionResolveView(APIView):
    """
    Approve, reject (revert to previous slot values), or override slot values for a pending upload revision.
    """

    permission_classes = [IsAuthenticated, IsConsumerManager]

    def _validate_view_date(self, value: datetime.date) -> datetime.date:
        today = timezone.localdate()
        if value > today:
            raise ValueError('You can only act on revisions up to today.')
        return value

    @transaction.atomic
    def post(self, request):
        expire_pending_generator_supply_upload_revisions()

        revision_id = request.data.get('revision_id')
        action = (request.data.get('action') or '').strip().lower()
        if revision_id is None:
            return Response({'detail': 'revision_id is required.'}, status=status.HTTP_400_BAD_REQUEST)
        if action not in ('approve', 'reject', 'override'):
            return Response(
                {'detail': 'action must be one of: approve, reject, override.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            revision_pk = int(revision_id)
        except (TypeError, ValueError):
            return Response({'detail': 'revision_id must be an integer.'}, status=status.HTTP_400_BAD_REQUEST)

        consumer = getattr(request.user, 'managed_consumer', None)
        if consumer is None:
            return Response({'detail': 'Consumer not linked for this manager user.'}, status=status.HTTP_400_BAD_REQUEST)

        rev = (
            GeneratorSupplyUploadRevision.objects.select_for_update()
            .select_related('schedule')
            .filter(id=revision_pk, schedule__consumer=consumer)
            .first()
        )
        if rev is None:
            return Response({'detail': 'Revision not found.'}, status=status.HTTP_404_NOT_FOUND)

        if rev.cm_review_status != GeneratorSupplyUploadRevision.CMReviewStatus.PENDING:
            return Response({'detail': 'This revision is not awaiting review.'}, status=status.HTTP_400_BAD_REQUEST)

        schedule = rev.schedule
        date_val = schedule.date
        try:
            self._validate_view_date(date_val)
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        tz_off = _clamp_tz_offset_minutes(request.data.get('client_tz_offset_minutes'))
        _, locked_through = generator_supply_revision_edit_bounds(date_val, tz_off)

        run = ConsumerGeneratorAllocationRun.objects.filter(consumer=consumer, date=date_val).first()
        allocation_approved = bool(run and run.status == ConsumerGeneratorAllocationRun.Status.APPROVED)

        now = timezone.now()
        resolved_fields = ['cm_review_status', 'resolved_at', 'resolved_by', 'deadline_at']

        if action == 'approve':
            rev.cm_review_status = GeneratorSupplyUploadRevision.CMReviewStatus.APPROVED
            rev.resolved_at = now
            rev.resolved_by = request.user
            rev.deadline_at = None
            rev.save(update_fields=resolved_fields)
            return Response(
                {
                    'revision_id': rev.id,
                    'action': action,
                    'cm_review_status': rev.cm_review_status,
                    'date': date_val,
                },
                status=status.HTTP_200_OK,
            )

        if action == 'reject':
            for d in rev.deltas.all():
                GeneratorSupplySlot.objects.filter(schedule=schedule, slot_index=d.slot_index).update(
                    supply_mwh=d.previous_mwh
                )
            rev.cm_review_status = GeneratorSupplyUploadRevision.CMReviewStatus.REJECTED
            rev.resolved_at = now
            rev.resolved_by = request.user
            rev.deadline_at = None
            rev.save(update_fields=resolved_fields)
            return Response(
                {
                    'revision_id': rev.id,
                    'action': action,
                    'cm_review_status': rev.cm_review_status,
                    'date': date_val,
                },
                status=status.HTTP_200_OK,
            )

        slots_payload = request.data.get('slots')
        if not slots_payload or not isinstance(slots_payload, list):
            return Response({'detail': 'override requires a non-empty slots array.'}, status=status.HTTP_400_BAD_REQUEST)

        delta_by_idx = {int(d.slot_index): d for d in rev.deltas.all()}
        if not delta_by_idx:
            return Response({'detail': 'This revision has no slot deltas to override.'}, status=status.HTTP_400_BAD_REQUEST)

        for item in slots_payload:
            try:
                idx = int(item.get('slot_index'))
            except (TypeError, ValueError):
                return Response({'detail': 'Each slot must include a valid slot_index.'}, status=status.HTTP_400_BAD_REQUEST)
            if idx not in delta_by_idx:
                return Response(
                    {'detail': f'Slot {idx} is not part of this revision.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if allocation_approved and locked_through and idx <= locked_through:
                return Response(
                    {'detail': f'Slot {idx} is locked and cannot be overridden.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            try:
                val = Decimal(str(item.get('supply_mwh')))
            except Exception:
                return Response({'detail': f'Invalid supply_mwh for slot {idx}.'}, status=status.HTTP_400_BAD_REQUEST)
            if val < 0:
                return Response({'detail': f'supply_mwh must be >= 0 for slot {idx}.'}, status=status.HTTP_400_BAD_REQUEST)

            GeneratorSupplySlot.objects.filter(schedule=schedule, slot_index=idx).update(supply_mwh=val)
            du = delta_by_idx[idx]
            du.new_mwh = val
            du.save(update_fields=['new_mwh'])

        rev.cm_review_status = GeneratorSupplyUploadRevision.CMReviewStatus.OVERRIDDEN
        rev.resolved_at = now
        rev.resolved_by = request.user
        rev.deadline_at = None
        rev.save(update_fields=resolved_fields)

        return Response(
            {
                'revision_id': rev.id,
                'action': action,
                'cm_review_status': rev.cm_review_status,
                'date': date_val,
                'slots_updated': len(slots_payload),
            },
            status=status.HTTP_200_OK,
        )


class GeneratorSupplyScheduleView(APIView):
    """
    Generator submits supply values (96 slots) for a consumer's day.
    On MVP, we treat submitted supply as "allocated" for consumer allocation UI.
    """

    permission_classes = [IsAuthenticated, IsGenerator]

    def _validate_date_range(self, value: datetime.date) -> datetime.date:
        today = timezone.localdate()
        max_date = today + datetime.timedelta(days=7)
        if value < today:
            raise ValueError('Supply is only available from today onwards.')
        if value > max_date:
            raise ValueError('You can only submit supply for today and the next 7 calendar days (8 days total).')
        return value

    @transaction.atomic
    def post(self, request):
        serializer = GeneratorSupplySubmitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        consumer_manager_user_id = data['consumer_manager_user_id']
        date_val = data['date']
        self._validate_date_range(date_val)

        consumer = get_object_or_404(
            Consumer.objects.select_related('consumer_manager'), consumer_manager_id=consumer_manager_user_id
        )

        tz_off = _clamp_tz_offset_minutes(request.data.get('client_tz_offset_minutes'))

        shutdown = bool(data.get('shutdown', False))

        schedule, created = GeneratorSupplySchedule.objects.get_or_create(
            consumer=consumer,
            date=date_val,
            defaults={'submitted_by_user': request.user},
        )
        if not created:
            schedule.submitted_by_user = request.user
        schedule.save(update_fields=['submitted_by_user'])

        existing_map = {int(s.slot_index): float(s.supply_mwh) for s in schedule.slots.all()}
        incoming_map = {int(s['slot_index']): float(s['supply_mwh']) for s in data['slots']}

        run = ConsumerGeneratorAllocationRun.objects.filter(consumer=consumer, date=date_val).first()
        allocation_approved = bool(run and run.status == ConsumerGeneratorAllocationRun.Status.APPROVED)
        if allocation_approved:
            if not generator_supply_planning_date_is_user_today(date_val, tz_off):
                return Response(
                    {'detail': 'This day is approved by Consumer Manager. Supply schedule is locked.'},
                    status=status.HTTP_403_FORBIDDEN,
                )
            if not existing_map:
                return Response(
                    {'detail': 'No saved schedule to update for this day.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            _, locked_through = generator_supply_revision_edit_bounds(date_val, tz_off)
            for idx in range(1, locked_through + 1):
                ex = existing_map.get(idx, 0.0)
                inc = incoming_map.get(idx, 0.0)
                if abs(ex - inc) > 1e-4:
                    return Response(
                        {
                            'detail': (
                                f'Slots 1–{locked_through} are locked after Consumer Manager approval. '
                                f'Slot {idx} must remain {ex:.4f} MWh (received {inc:.4f}).'
                            )
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )

        GeneratorSupplySlot.objects.filter(schedule=schedule).delete()

        expected_slots = generate_day_slots()
        slots_by_index = {s['slot_index']: s['supply_mwh'] for s in data['slots']}

        bulk = []
        for slot in expected_slots:
            idx = slot['slot_index']
            supply_val = 0 if shutdown else slots_by_index.get(idx, 0)
            bulk.append(
                GeneratorSupplySlot(
                    schedule=schedule,
                    slot_index=idx,
                    slot_time=slot['slot_time'],
                    supply_mwh=supply_val,
                )
            )
        GeneratorSupplySlot.objects.bulk_create(bulk)

        deltas: list[tuple[int, float, float]] = []
        for idx in range(1, 97):
            old_v = existing_map.get(idx, 0.0)
            new_v = float(incoming_map.get(idx, 0.0))
            if abs(old_v - new_v) > 1e-4:
                deltas.append((idx, old_v, new_v))

        if deltas and existing_map:
            _auto_resolve_stacked_pending_upload_revisions(schedule)
            next_no = (
                GeneratorSupplyUploadRevision.objects.filter(schedule=schedule).aggregate(m=Max('revision_number'))['m']
                or 0
            ) + 1
            pending_cm = allocation_approved
            deadline_at = None
            resolved_at = None
            cm_status = GeneratorSupplyUploadRevision.CMReviewStatus.APPROVED
            if pending_cm:
                cm_status = GeneratorSupplyUploadRevision.CMReviewStatus.PENDING
                deadline_at = timezone.now() + datetime.timedelta(minutes=GENERATOR_UPLOAD_REVISION_CM_WINDOW_MINUTES)
            else:
                resolved_at = timezone.now()
            rev = GeneratorSupplyUploadRevision.objects.create(
                schedule=schedule,
                revision_number=next_no,
                created_by=request.user,
                cm_review_status=cm_status,
                deadline_at=deadline_at,
                resolved_at=resolved_at,
            )
            GeneratorSupplyUploadRevisionDelta.objects.bulk_create(
                [
                    GeneratorSupplyUploadRevisionDelta(
                        revision=rev,
                        slot_index=idx,
                        previous_mwh=Decimal(str(round(prev, 4))),
                        new_mwh=Decimal(str(round(new_v, 4))),
                    )
                    for idx, prev, new_v in deltas
                ]
            )

        return Response(
            {
                'consumer_manager_user_id': int(consumer_manager_user_id),
                'consumer_name': consumer.name,
                'energy_manager_name': energy_manager_display_name(consumer),
                'date': date_val,
                'saved': True,
            },
            status=status.HTTP_200_OK,
        )

    def get(self, request):
        expire_pending_generator_supply_upload_revisions()
        consumer_manager_user_id = request.query_params.get('consumer_manager_user_id')
        date_str = request.query_params.get('date')
        if not consumer_manager_user_id or not date_str:
            return Response({'detail': 'consumer_manager_user_id and date are required.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            req_date = datetime.date.fromisoformat(date_str)
            self._validate_date_range(req_date)
        except ValueError:
            return Response({'detail': 'Invalid date format. Use YYYY-MM-DD.'}, status=status.HTTP_400_BAD_REQUEST)

        consumer = get_object_or_404(
            Consumer.objects.select_related('consumer_manager'), consumer_manager_id=consumer_manager_user_id
        )
        schedule = GeneratorSupplySchedule.objects.filter(consumer=consumer, date=req_date).first()
        run = ConsumerGeneratorAllocationRun.objects.filter(consumer=consumer, date=req_date).first()
        allocation_approved = bool(run and run.status == ConsumerGeneratorAllocationRun.Status.APPROVED)
        generator_schedule_approved = GeneratorScheduleApproval.objects.filter(consumer=consumer, date=req_date).exists()
        # Schedule revisions page: show revision data when CM approved the generator schedule or full day allocation.
        revisions_approved = bool(generator_schedule_approved or allocation_approved)

        slot_map = {}
        if schedule:
            for s in schedule.slots.all():
                slot_map[s.slot_index] = s.supply_mwh

        # Per-slot allocation revision label (SlotAllocationApproval), if CM approved slot allocations.
        approval_groups: defaultdict[int, list] = defaultdict(list)
        for row in SlotAllocationApproval.objects.filter(consumer=consumer, date=req_date):
            approval_groups[row.slot_index].append(row)
        slot_revision_map: dict[int, str] = {}
        for idx, rows in approval_groups.items():
            slot_revision_map[int(idx)] = rows[0].approved_revision if rows else ''

        rev_order = ('submitted', 'revision1', 'revision3', 'revision2')
        rev_labels = {
            'submitted': 'Submitted schedule',
            'revision1': 'Revision 1',
            'revision3': 'Revision 3',
            # revision2 is the approved allocation snapshot (legacy enum key name).
            'revision2': 'Approved allocation',
        }
        distinct_keys = sorted(
            {v for v in slot_revision_map.values() if v},
            key=lambda x: rev_order.index(x) if x in rev_order else 99,
        )
        allocation_revision_summary = [{'key': k, 'label': rev_labels.get(k, k)} for k in distinct_keys]

        resp_slots = []
        for slot in generate_day_slots():
            idx = slot['slot_index']
            resp_slots.append(
                {
                    'slot_index': idx,
                    'slot_time': slot['slot_time'].strftime('%H:%M'),
                    'time_block': slot['time_block'],
                    'supply_mwh': slot_map.get(idx, 0),
                    'allocation_revision': slot_revision_map.get(idx),
                }
            )

        tz_off_get = _clamp_tz_offset_minutes(request.query_params.get('client_tz_offset_minutes'))
        first_editable, locked_through = generator_supply_revision_edit_bounds(req_date, tz_off_get)
        editable_slot_range = {
            'first_editable_slot': first_editable,
            'locked_through_slot': locked_through,
            'buffer_slots': GENERATOR_SUPPLY_REVISION_BUFFER_SLOTS,
        }

        generator_upload_revisions: list[dict] = []
        if schedule:
            now_ts = timezone.now()
            for ur in (
                GeneratorSupplyUploadRevision.objects.filter(schedule=schedule)
                .order_by('revision_number')
                .prefetch_related('deltas')
            ):
                row = _serialize_generator_upload_revision_row(ur, now_ts)
                row['id'] = ur.id
                generator_upload_revisions.append(row)

        return Response(
            {
                'consumer_manager_user_id': int(consumer_manager_user_id),
                'consumer_name': consumer.name,
                'energy_manager_name': energy_manager_display_name(consumer),
                'date': req_date,
                'exists': schedule is not None,
                'allocation_approved': allocation_approved,
                'generator_schedule_approved': generator_schedule_approved,
                'revisions_approved': revisions_approved,
                'allocation_revision_summary': allocation_revision_summary,
                'editable_slot_range': editable_slot_range,
                'generator_upload_revisions': generator_upload_revisions,
                'slots': resp_slots,
            },
            status=status.HTTP_200_OK,
        )

    @transaction.atomic
    def delete(self, request):
        consumer_manager_user_id = request.query_params.get('consumer_manager_user_id')
        date_str = request.query_params.get('date')
        if not consumer_manager_user_id or not date_str:
            return Response({'detail': 'consumer_manager_user_id and date are required.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            req_date = datetime.date.fromisoformat(date_str)
            self._validate_date_range(req_date)
        except ValueError:
            return Response({'detail': 'Invalid date format. Use YYYY-MM-DD.'}, status=status.HTTP_400_BAD_REQUEST)

        consumer = get_object_or_404(
            Consumer.objects.select_related('consumer_manager'), consumer_manager_id=consumer_manager_user_id
        )

        run = ConsumerGeneratorAllocationRun.objects.filter(consumer=consumer, date=req_date).first()
        allocation_approved = bool(run and run.status == ConsumerGeneratorAllocationRun.Status.APPROVED)
        if allocation_approved:
            return Response(
                {'detail': 'This day is approved by Consumer Manager. Supply schedule cannot be deleted.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        schedule = GeneratorSupplySchedule.objects.filter(consumer=consumer, date=req_date).first()
        if not schedule:
            return Response({'detail': 'No schedule exists for this day.'}, status=status.HTTP_200_OK)

        schedule.delete()
        return Response(
            {
                'consumer_manager_user_id': int(consumer_manager_user_id),
                'consumer_name': consumer.name,
                'energy_manager_name': energy_manager_display_name(consumer),
                'date': req_date,
                'deleted': True,
            },
            status=status.HTTP_200_OK,
        )


class GeneratorDashboardKaiView(APIView):
    """
    Kaiadmin generator dashboard: KPIs (today, real time), histogram series, plant approved (focus day).
    graph_range: yesterday | today | tomorrow | next7. Next 7 days: daily bins from today for 7 days; hourly for single-day.
    plant_id: optional — total approved gross MWh for that plant for the same focus day as the histogram.
    """

    permission_classes = [IsAuthenticated, IsGenerator]

    def get(self, request):
        graph_range = (request.query_params.get('graph_range') or 'next7').lower()
        if graph_range not in ('yesterday', 'today', 'tomorrow', 'next7'):
            graph_range = 'next7'

        today = timezone.localdate()
        tomorrow = today + datetime.timedelta(days=1)
        yesterday = today - datetime.timedelta(days=1)
        max_day = tomorrow + datetime.timedelta(days=6)

        generator_user = request.user
        # Only consumers this generator has actually submitted a supply schedule for (matches one CM portfolio when one consumer).
        linked_cids = list(
            dict.fromkeys(
                GeneratorSupplySchedule.objects.filter(submitted_by_user=generator_user).values_list('consumer_id', flat=True)
            )
        )
        consumers = list(Consumer.objects.filter(id__in=linked_cids).order_by('id')) if linked_cids else []

        kpi_dates = [today]
        days_count_dates = [today + datetime.timedelta(days=i) for i in range(7)]
        total_approved = Decimal('0')
        total_supply = Decimal('0')
        total_iex = Decimal('0')
        days_with_schedule = set()
        # Reuse one allocation context per (consumer, date) in this request (KPI + histogram + plant card).
        alloc_ctx_by_consumer_date = {}

        for d in kpi_dates:
            for c in consumers:
                pre_ctx = None
                if _generator_day_needs_allocation_context(c, d):
                    pre_ctx = load_consumer_allocation_slot_context(c, d)
                    alloc_ctx_by_consumer_date[(c.id, d)] = pre_ctx
                a, s, ix = generator_approved_allocation_day_totals(
                    c, d, generator_user, allocation_context=pre_ctx
                )
                total_approved += a
                total_supply += s
                total_iex += ix
            if GeneratorSupplySchedule.objects.filter(submitted_by_user=generator_user, date=d).exists():
                days_with_schedule.add(d)
        for d in days_count_dates:
            if GeneratorSupplySchedule.objects.filter(submitted_by_user=generator_user, date=d).exists():
                days_with_schedule.add(d)

        iex_kpi_display = str(round(total_iex, 4)) if total_iex > Decimal('1') else '0'

        if graph_range == 'yesterday':
            series_dates = [yesterday]
            single_day_for_hist = yesterday
        elif graph_range == 'today':
            series_dates = [today]
            single_day_for_hist = today
        elif graph_range == 'tomorrow':
            series_dates = [tomorrow]
            single_day_for_hist = tomorrow
        elif graph_range == 'next7':
            series_dates = [today + datetime.timedelta(days=i) for i in range(7)]
            single_day_for_hist = None
        else:
            series_dates = [today + datetime.timedelta(days=i) for i in range(7)]
            single_day_for_hist = None

        # Right-hand plant card: same day as the histogram, or "today" when the chart is next-7.
        plant_focus_date = single_day_for_hist if single_day_for_hist is not None else today
        if graph_range == 'yesterday':
            plant_card_day_label = 'Yesterday'
            plant_gross_subtitle = 'yesterday'
        elif graph_range == 'tomorrow':
            plant_card_day_label = 'Tomorrow'
            plant_gross_subtitle = 'tomorrow'
        elif graph_range == 'today':
            plant_card_day_label = 'Real time'
            plant_gross_subtitle = 'today'
        else:
            plant_card_day_label = 'Real time'
            plant_gross_subtitle = 'today'

        series_labels: list[str] = []
        supply_series: list[float] = []
        alloc_series: list[float] = []
        granularity = 'day'

        if graph_range == 'next7':
            granularity = 'day'
            for d in series_dates:
                sup_d = Decimal('0')
                alloc_d = Decimal('0')
                for c in consumers:
                    pre = None
                    if _generator_day_needs_allocation_context(c, d):
                        pre = load_consumer_allocation_slot_context(c, d)
                    a, s, _ = generator_approved_allocation_day_totals(
                        c, d, generator_user, allocation_context=pre
                    )
                    sup_d += s
                    alloc_d += a
                series_labels.append(d.strftime('%a %d %b'))
                supply_series.append(float(round(sup_d, 4)))
                alloc_series.append(float(round(alloc_d, 4)))
        else:
            granularity = 'hour'
            sup_acc_s = [Decimal('0')] * int(SLOTS_PER_DAY)
            sup_acc_a = [Decimal('0')] * int(SLOTS_PER_DAY)
            for c in consumers:
                pre_ctx = alloc_ctx_by_consumer_date.get((c.id, single_day_for_hist))
                sl, al = generator_day_slot_supply_and_accounted(
                    c, single_day_for_hist, generator_user, allocation_context=pre_ctx
                )
                for i in range(int(SLOTS_PER_DAY)):
                    sup_acc_s[i] += sl[i]
                    sup_acc_a[i] += al[i]
            supply_series, alloc_series = _slots_to_hourly(sup_acc_s, sup_acc_a)
            series_labels = [f'{h:02d}:00' for h in range(24)]

        plant_id_param = request.query_params.get('plant_id')
        plant_tomorrow_block = None
        plants_payload: list[dict] = []

        consumer_ids_qs = GeneratorSupplySchedule.objects.filter(submitted_by_user=generator_user).values_list(
            'consumer_id', flat=True
        )
        consumer_ids = list(dict.fromkeys(consumer_ids_qs))
        plant_qs = Plant.objects.filter(consumer_id__in=consumer_ids).order_by('name') if consumer_ids else Plant.objects.none()
        if not plant_qs.exists():
            plant_qs = Plant.objects.select_related('consumer').order_by('consumer__name', 'name')

        for pl in plant_qs:
            plants_payload.append({'id': pl.id, 'name': pl.name})

        if plant_id_param:
            try:
                pid = int(plant_id_param)
            except ValueError:
                pid = 0
            if pid > 0:
                plant = plant_qs.filter(id=pid).select_related('consumer').first()
                if plant:
                    slot_vals = generator_plant_approved_gross_slots_mwh(
                        plant.consumer,
                        plant_focus_date,
                        plant.id,
                        allocation_context=alloc_ctx_by_consumer_date.get((plant.consumer_id, plant_focus_date)),
                    )
                    appr = sum(slot_vals, start=Decimal('0'))
                    slot_labels = [s['slot_time'].strftime('%H:%M') for s in generate_day_slots()]
                    plant_tomorrow_block = {
                        'plant_id': plant.id,
                        'plant_name': plant.name,
                        'date': plant_focus_date.isoformat(),
                        'day_label': plant_card_day_label,
                        'approved_mwh': str(round(appr, 4)),
                        'slot_labels': slot_labels,
                        'approved_mwh_slots': [str(round(float(v), 6)) for v in slot_vals],
                    }

        if graph_range == 'yesterday':
            graph_heading_bracket = 'Yesterday'
        elif graph_range == 'today':
            graph_heading_bracket = 'Today'
        elif graph_range == 'tomorrow':
            graph_heading_bracket = 'Tomorrow'
        else:
            graph_heading_bracket = 'Next 7 days'

        kpi_heading_bracket = 'Real time'

        return Response(
            {
                'kpis': {
                    'total_approved_supply_mwh': str(round(total_approved, 4)),
                    'total_supply_scheduled_mwh': str(round(total_supply, 4)),
                    'iex_allocations_mwh': str(round(total_iex, 4)),
                    'iex_allocations_display_mwh': iex_kpi_display,
                    'total_days_scheduled': len(days_with_schedule),
                    'heading_bracket': kpi_heading_bracket,
                },
                'window': {
                    'start': today.isoformat(),
                    'end': today.isoformat(),
                    'label': f'KPI (Real time) — {today.isoformat()}',
                },
                'graph_range': graph_range,
                'graph_heading_bracket': graph_heading_bracket,
                'plant_card_meta': {
                    'focus_date': plant_focus_date.isoformat(),
                    'day_label': plant_card_day_label,
                    'gross_subtitle': plant_gross_subtitle,
                },
                'series': {
                    'labels': series_labels,
                    'supply_mwh': supply_series,
                    'allocated_mwh': alloc_series,
                    'granularity': granularity,
                },
                'plants': plants_payload,
                'plant_tomorrow': plant_tomorrow_block,
            },
            status=status.HTTP_200_OK,
        )


class PlantDashboardKaiView(APIView):
    """
    Kaiadmin plant user dashboard: same response shape as GeneratorDashboardKaiView, scoped to the logged-in
    plant user's plant and that plant's consumer. Generator supply and IEX use the consumer's schedule
    (unique per consumer+date). Histogram "allocated" series is this plant's approved gross MWh only.
    """

    permission_classes = [IsAuthenticated, IsPlantUser]

    def get(self, request):
        graph_range = (request.query_params.get('graph_range') or 'next7').lower()
        if graph_range == 'today':
            graph_range = 'next7'

        plant = get_plant_for_plant_user(request.user)
        consumer = plant.consumer

        today = timezone.localdate()
        tomorrow = today + datetime.timedelta(days=1)
        yesterday = today - datetime.timedelta(days=1)
        max_day = tomorrow + datetime.timedelta(days=6)

        kpi_dates = [tomorrow]
        days_count_dates = [tomorrow + datetime.timedelta(days=i) for i in range(7)]
        total_approved_plant = Decimal('0')
        total_supply = Decimal('0')
        total_iex = Decimal('0')
        days_with_schedule = set()

        for d in kpi_dates:
            _, s, ix = generator_approved_allocation_day_totals(consumer, d, None)
            total_supply += s
            total_iex += ix
            total_approved_plant += generator_plant_approved_gross_mwh(consumer, d, plant.id)
            if DemandSchedule.objects.filter(plant=plant, date=d).exists():
                days_with_schedule.add(d)
        for d in days_count_dates:
            if DemandSchedule.objects.filter(plant=plant, date=d).exists():
                days_with_schedule.add(d)

        iex_kpi_display = str(round(total_iex, 4)) if total_iex > Decimal('1') else '0'

        if graph_range == 'yesterday':
            series_dates = [yesterday]
            single_day_for_hist = yesterday
        elif graph_range == 'tomorrow':
            series_dates = [tomorrow]
            single_day_for_hist = tomorrow
        elif graph_range == 'next7':
            series_dates = [tomorrow + datetime.timedelta(days=i) for i in range(7)]
            single_day_for_hist = None
        else:
            series_dates = [tomorrow + datetime.timedelta(days=i) for i in range(7)]
            single_day_for_hist = None

        plant_focus_date = single_day_for_hist if single_day_for_hist is not None else tomorrow
        if graph_range == 'yesterday':
            plant_card_day_label = 'Yesterday'
        elif graph_range == 'tomorrow':
            plant_card_day_label = 'Tomorrow'
        else:
            plant_card_day_label = 'Tomorrow'

        series_labels: list[str] = []
        supply_series: list[float] = []
        alloc_series: list[float] = []
        granularity = 'day'

        if graph_range == 'next7':
            granularity = 'day'
            for d in series_dates:
                _, sup_d, _ = generator_approved_allocation_day_totals(consumer, d, None)
                alloc_d = generator_plant_approved_gross_mwh(consumer, d, plant.id)
                series_labels.append(d.strftime('%a %d %b'))
                supply_series.append(float(round(sup_d, 4)))
                alloc_series.append(float(round(alloc_d, 4)))
        else:
            granularity = 'hour'
            sl, _ = generator_day_slot_supply_and_accounted(consumer, single_day_for_hist, None)
            plant_slots = generator_plant_approved_gross_slots_mwh(consumer, single_day_for_hist, plant.id)
            supply_series, alloc_series = _slots_to_hourly(sl, plant_slots)
            series_labels = [f'{h:02d}:00' for h in range(24)]

        plants_payload = [{'id': plant.id, 'name': plant.name}]

        slot_vals = generator_plant_approved_gross_slots_mwh(consumer, plant_focus_date, plant.id)
        appr = sum(slot_vals, start=Decimal('0'))
        slot_labels = [s['slot_time'].strftime('%H:%M') for s in generate_day_slots()]
        plant_tomorrow_block = {
            'plant_id': plant.id,
            'plant_name': plant.name,
            'date': plant_focus_date.isoformat(),
            'day_label': plant_card_day_label,
            'approved_mwh': str(round(appr, 4)),
            'slot_labels': slot_labels,
            'approved_mwh_slots': [str(round(float(v), 6)) for v in slot_vals],
        }

        kpi_heading_bracket = 'tomorrow'
        if graph_range == 'yesterday':
            graph_heading_bracket = 'Yesterday'
        elif graph_range == 'tomorrow':
            graph_heading_bracket = 'Tomorrow'
        else:
            graph_heading_bracket = 'Next 7 days'

        return Response(
            {
                'kpis': {
                    'total_approved_supply_mwh': str(round(total_approved_plant, 4)),
                    'total_supply_scheduled_mwh': str(round(total_supply, 4)),
                    'iex_allocations_mwh': str(round(total_iex, 4)),
                    'iex_allocations_display_mwh': iex_kpi_display,
                    'total_days_scheduled': len(days_with_schedule),
                    'heading_bracket': kpi_heading_bracket,
                },
                'window': {
                    'start': tomorrow.isoformat(),
                    'end': tomorrow.isoformat(),
                    'label': f'KPI day (tomorrow): {tomorrow.isoformat()}',
                },
                'graph_range': graph_range,
                'graph_heading_bracket': graph_heading_bracket,
                'plant_card_meta': {
                    'focus_date': plant_focus_date.isoformat(),
                    'day_label': plant_card_day_label,
                },
                'series': {
                    'labels': series_labels,
                    'supply_mwh': supply_series,
                    'allocated_mwh': alloc_series,
                    'granularity': granularity,
                },
                'plants': plants_payload,
                'plant_tomorrow': plant_tomorrow_block,
            },
            status=status.HTTP_200_OK,
        )


class ConsumerManagerDashboardKpiView(APIView):
    """
    Energy manager dashboard: aggregate KPIs for the managed consumer over the next 7 days
    (from tomorrow): total generator supply, total approved plant gross allocation, total demand (gross),
    and count of days with CM-approved allocation runs.
    """

    permission_classes = [IsAuthenticated, IsConsumerManager]

    def get(self, request):
        consumer = get_managed_consumer(request.user)
        if consumer is None:
            return Response({'detail': 'Consumer not linked for this manager user.'}, status=status.HTTP_400_BAD_REQUEST)

        today = timezone.localdate()
        tomorrow = today + datetime.timedelta(days=1)
        max_day = tomorrow + datetime.timedelta(days=6)
        kpi_dates = [tomorrow + datetime.timedelta(days=i) for i in range(7)]

        plants = list(Plant.objects.filter(consumer=consumer).order_by('id'))
        total_supply = Decimal('0')
        total_approved = Decimal('0')
        total_demand = Decimal('0')
        days_approved = 0

        for d in kpi_dates:
            _, s, _ = generator_approved_allocation_day_totals(consumer, d, None)
            total_supply += s
            for p in plants:
                total_approved += generator_plant_approved_gross_mwh(consumer, d, p.id)
                total_demand += plant_demand_gross_mwh_total(consumer, d, p.id)

            run = ConsumerGeneratorAllocationRun.objects.filter(consumer=consumer, date=d).first()
            if run and run.status == ConsumerGeneratorAllocationRun.Status.APPROVED:
                days_approved += 1

        return Response(
            {
                'kpis': {
                    'overall_supply_mwh': str(round(total_supply, 4)),
                    'overall_approved_mwh': str(round(total_approved, 4)),
                    'overall_demand_mwh': str(round(total_demand, 4)),
                    'overall_days_approved': days_approved,
                    'heading_bracket': 'next 7 days',
                },
                'window': {
                    'start': tomorrow.isoformat(),
                    'end': max_day.isoformat(),
                    'label': f'{tomorrow.isoformat()} → {max_day.isoformat()}',
                },
            },
            status=status.HTTP_200_OK,
        )


def _consumer_slot_demand_gross_96(consumer, req_date) -> list[Decimal]:
    """96-slot gross demand (all plants) for one day, index 0 = slot 1."""
    from core.api.serializers import get_transmission_loss_percent, net_to_gross

    plants = Plant.objects.filter(consumer=consumer)
    year = req_date.year
    slot_totals: dict[int, Decimal] = {}
    for plant in plants:
        schedule = DemandSchedule.objects.filter(plant=plant, date=req_date).first()
        if not schedule or schedule.shutdown:
            continue
        loss_pct = get_transmission_loss_percent(plant, year)
        for s in schedule.slots.all():
            net_val = Decimal(str(s.demand_mw or 0))
            gross_val = net_to_gross(net_val, loss_pct)
            idx = int(s.slot_index)
            slot_totals[idx] = slot_totals.get(idx, Decimal('0')) + gross_val
    return [slot_totals.get(i, Decimal('0')) for i in range(1, int(SLOTS_PER_DAY) + 1)]


def _consumer_slot_supply_96(consumer, req_date) -> list[Decimal]:
    """96-slot generator supply for the consumer/day, index 0 = slot 1."""
    supply_by_slot: dict[int, Decimal] = {}
    gss = GeneratorSupplySchedule.objects.filter(consumer=consumer, date=req_date).first()
    if gss:
        for s in gss.slots.all():
            supply_by_slot[int(s.slot_index)] = Decimal(str(s.supply_mwh or 0))
    return [supply_by_slot.get(i, Decimal('0')) for i in range(1, int(SLOTS_PER_DAY) + 1)]


def _series96_to_hourly(vals96: list[Decimal]) -> list[float]:
    out: list[float] = []
    for h in range(24):
        lo = h * 4
        hi = lo + 4
        out.append(float(round(sum(vals96[lo:hi], start=Decimal('0')), 4)))
    return out


def _plant_slot_demand_gross_96(consumer, req_date, plant_id: int) -> list[Decimal]:
    """96-slot gross demand for one plant/day (index 0 = slot 1)."""
    from core.api.serializers import get_transmission_loss_percent, net_to_gross

    plant = Plant.objects.filter(consumer=consumer, id=plant_id).first()
    if not plant:
        return [Decimal('0')] * int(SLOTS_PER_DAY)
    year = req_date.year
    schedule = DemandSchedule.objects.filter(plant=plant, date=req_date).first()
    if not schedule or schedule.shutdown:
        return [Decimal('0')] * int(SLOTS_PER_DAY)
    loss_pct = get_transmission_loss_percent(plant, year)
    slot_map: dict[int, Decimal] = {}
    for s in schedule.slots.all():
        net_val = Decimal(str(s.demand_mw or 0))
        gross_val = net_to_gross(net_val, loss_pct)
        slot_map[int(s.slot_index)] = gross_val
    return [slot_map.get(i, Decimal('0')) for i in range(1, int(SLOTS_PER_DAY) + 1)]


def _consumer_manager_plant_slot_panel_payload(consumer, req_date: datetime.date, plant_id: int):
    """96×15-min slot series for one plant/day (approved gross supply, gross demand, |gap|)."""
    plants = list(Plant.objects.filter(consumer=consumer).order_by('id'))
    if not plants:
        return None, []
    if not any(p.id == plant_id for p in plants):
        plant_id = plants[0].id
    sup_slots = generator_plant_approved_gross_slots_mwh(consumer, req_date, plant_id)
    dem_slots = _plant_slot_demand_gross_96(consumer, req_date, plant_id)
    n = int(SLOTS_PER_DAY)
    diff_slots: list[float] = []
    for i in range(n):
        sv = float(sup_slots[i]) if i < len(sup_slots) else 0.0
        dv = float(dem_slots[i]) if i < len(dem_slots) else 0.0
        diff_slots.append(round(abs(sv - dv), 6))
    slot_labels = [s['slot_time'].strftime('%H:%M') for s in generate_day_slots()]
    panel = {
        'plant_id': plant_id,
        'slot_labels': slot_labels,
        'supply_mwh': [round(float(sup_slots[i]), 6) if i < len(sup_slots) else 0.0 for i in range(n)],
        'demand_mwh': [round(float(dem_slots[i]), 6) if i < len(dem_slots) else 0.0 for i in range(n)],
        'difference_mwh': diff_slots,
    }
    return panel, [{'id': p.id, 'name': p.name} for p in plants]


class ConsumerManagerDashboardKaiView(APIView):
    """
    Energy manager Kaiadmin dashboard: main chart = supply, demand, |difference| (same layout as generator).
    Right panel = selected plant, 96×15-min slots: approved supply (bars), demand & |gap| (lines).
    Optional: plant_id for right panel (defaults to first plant).
    graph_range: yesterday | tomorrow | next7
    """

    permission_classes = [IsAuthenticated, IsConsumerManager]

    def get(self, request):
        graph_range = (request.query_params.get('graph_range') or 'next7').lower()
        if graph_range == 'today':
            graph_range = 'next7'

        plant_id_param = request.query_params.get('plant_id')
        selected_pid: int | None = None
        if plant_id_param not in (None, ''):
            try:
                selected_pid = int(plant_id_param)
            except ValueError:
                selected_pid = None

        consumer = get_managed_consumer(request.user)
        if consumer is None:
            return Response({'detail': 'Consumer not linked for this manager user.'}, status=status.HTTP_400_BAD_REQUEST)

        today = timezone.localdate()
        tomorrow = today + datetime.timedelta(days=1)
        yesterday = today - datetime.timedelta(days=1)
        max_day = tomorrow + datetime.timedelta(days=6)

        plants = list(Plant.objects.filter(consumer=consumer).order_by('id'))

        if graph_range == 'yesterday':
            series_dates = [yesterday]
            single_day_for_hist = yesterday
        elif graph_range == 'tomorrow':
            series_dates = [tomorrow]
            single_day_for_hist = tomorrow
        elif graph_range == 'next7':
            series_dates = [tomorrow + datetime.timedelta(days=i) for i in range(7)]
            single_day_for_hist = None
        else:
            series_dates = [tomorrow + datetime.timedelta(days=i) for i in range(7)]
            single_day_for_hist = None

        plant_focus_date = single_day_for_hist if single_day_for_hist is not None else tomorrow
        if graph_range == 'yesterday':
            plant_card_day_label = 'Yesterday'
        elif graph_range == 'tomorrow':
            plant_card_day_label = 'Tomorrow'
        else:
            plant_card_day_label = 'Tomorrow'

        series_labels: list[str] = []
        supply_series: list[float] = []
        demand_series: list[float] = []
        diff_series: list[float] = []
        granularity = 'day'

        if graph_range == 'next7':
            granularity = 'day'
            for d in series_dates:
                _, sup_d, _ = generator_approved_allocation_day_totals(consumer, d, None)
                dem_d = sum(
                    (plant_demand_gross_mwh_total(consumer, d, p.id) for p in plants),
                    start=Decimal('0'),
                )
                sd = float(round(sup_d, 4))
                dd = float(round(dem_d, 4))
                series_labels.append(d.strftime('%a %d %b'))
                supply_series.append(sd)
                demand_series.append(dd)
                diff_series.append(round(abs(sd - dd), 4))
        else:
            granularity = 'hour'
            sup96 = _consumer_slot_supply_96(consumer, single_day_for_hist)
            dem96 = _consumer_slot_demand_gross_96(consumer, single_day_for_hist)
            supply_series = _series96_to_hourly(sup96)
            demand_series = _series96_to_hourly(dem96)
            diff_series = [round(abs(supply_series[i] - demand_series[i]), 4) for i in range(24)]
            series_labels = [f'{h:02d}:00' for h in range(24)]

        if plants:
            if selected_pid is None or not any(p.id == selected_pid for p in plants):
                selected_pid = plants[0].id
        else:
            selected_pid = None

        plant_slot_panel: dict | None = None
        if selected_pid is not None:
            plant_slot_panel, _ = _consumer_manager_plant_slot_panel_payload(consumer, plant_focus_date, selected_pid)

        if graph_range == 'yesterday':
            graph_heading_bracket = 'Yesterday'
        elif graph_range == 'tomorrow':
            graph_heading_bracket = 'Tomorrow'
        else:
            graph_heading_bracket = 'Next 7 days'

        return Response(
            {
                'graph_range': graph_range,
                'graph_heading_bracket': graph_heading_bracket,
                'plant_card_meta': {
                    'focus_date': plant_focus_date.isoformat(),
                    'day_label': plant_card_day_label,
                },
                'series': {
                    'labels': series_labels,
                    'supply_mwh': supply_series,
                    'demand_mwh': demand_series,
                    'difference_mwh': diff_series,
                    'granularity': granularity,
                },
                'plant_slot_panel': plant_slot_panel,
                'plants': [{'id': p.id, 'name': p.name} for p in plants],
                'window': {
                    'start': tomorrow.isoformat(),
                    'end': max_day.isoformat(),
                },
            },
            status=status.HTTP_200_OK,
        )


class ConsumerManagerReportsPlantSlotsView(APIView):
    """
    Energy manager reports: one day × one plant, 96 quarter-hour slots.
    Query: date (YYYY-MM-DD), optional plant_id (defaults to first plant).
    """

    permission_classes = [IsAuthenticated, IsConsumerManager]

    def get(self, request):
        date_str = request.query_params.get('date')
        if not date_str:
            return Response({'detail': 'date query parameter is required (YYYY-MM-DD).'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            req_date = datetime.date.fromisoformat(date_str)
        except ValueError:
            return Response({'detail': 'Invalid date format. Use YYYY-MM-DD.'}, status=status.HTTP_400_BAD_REQUEST)

        plant_id_param = request.query_params.get('plant_id')
        selected_pid: int | None = None
        if plant_id_param not in (None, ''):
            try:
                selected_pid = int(plant_id_param)
            except ValueError:
                selected_pid = None

        consumer = get_managed_consumer(request.user)
        if consumer is None:
            return Response({'detail': 'Consumer not linked for this manager user.'}, status=status.HTTP_400_BAD_REQUEST)

        panel, plants = _consumer_manager_plant_slot_panel_payload(consumer, req_date, selected_pid if selected_pid is not None else 0)
        if panel is None:
            return Response(
                {
                    'date': req_date.isoformat(),
                    'plants': plants,
                    'plant_slot_panel': None,
                },
                status=status.HTTP_200_OK,
            )
        return Response(
            {
                'date': req_date.isoformat(),
                'plants': plants,
                'plant_slot_panel': panel,
            },
            status=status.HTTP_200_OK,
        )


class ConsumerAllocationSlotsView(APIView):
    """
    Consumer Manager sees demand vs allocated (generator supply) aggregated across all plants.
    """

    permission_classes = [IsAuthenticated, IsConsumerManager]

    def _validate_date_range(self, value: datetime.date) -> datetime.date:
        today = timezone.localdate()
        if value > today:
            raise ValueError('You can only view allocation up to today.')
        return value

    def get(self, request):
        date_str = request.query_params.get('date')
        if not date_str:
            return Response({'detail': 'date query parameter is required.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            req_date = datetime.date.fromisoformat(date_str)
        except ValueError:
            return Response({'detail': 'Invalid date format. Use YYYY-MM-DD.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            self._validate_date_range(req_date)
        except ValueError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)

        consumer = getattr(request.user, 'managed_consumer', None)
        if consumer is None:
            return Response({'detail': 'Consumer not linked for this manager user.'}, status=status.HTTP_400_BAD_REQUEST)
        # If there's an approved AI allocation run, apply its AI overrides.
        overrides_map: dict[int, Decimal] = {}
        run = ConsumerGeneratorAllocationRun.objects.filter(consumer=consumer, date=req_date).first()
        if run and run.status == ConsumerGeneratorAllocationRun.Status.APPROVED:
            overrides_map = {
                ov.plant_id: ov.ai_alloc_mwh_override_total
                for ov in ConsumerGeneratorAllocationOverride.objects.filter(run=run).select_related('plant')
            }

        try:
            mcp_map = ensure_iex_mcp_for_date(req_date)
        except Exception:
            mcp_map = None

        allocation_result = compute_allocation_with_ai_overrides(consumer, req_date, overrides_map, mcp_by_slot_index=mcp_map)

        total_demand = sum(Decimal(s['demand_mwh']) for s in allocation_result['slot_rows'])
        total_allocated = sum(Decimal(s['allocated_mwh']) for s in allocation_result['slot_rows'])
        total_generator_supply = sum(
            Decimal(s.get('supply_mwh') or '0') for s in allocation_result['slot_rows']
        )
        num_plants = len(allocation_result.get('plants') or [])

        return Response(
            {
                'consumer_id': consumer.id,
                'date': req_date,
                'total_demand_mwh': str(total_demand),
                'total_allocated_mwh': str(total_allocated),
                'total_generator_supply_mwh': str(total_generator_supply),
                'num_plants': num_plants,
                'slots': allocation_result['slot_rows'],
            },
            status=status.HTTP_200_OK,
        )


class ConsumerGeneratorAllocationGenerateView(APIView):
    """
    Creates/refreshes an AI allocation run (SUGGESTED) for a consumer+date.
    This resets overrides so the user can generate new recommendations.
    """

    permission_classes = [IsAuthenticated, IsConsumerManager]

    def _validate_date_range(self, value: datetime.date) -> datetime.date:
        today = timezone.localdate()
        tomorrow = today + datetime.timedelta(days=1)
        max_date = tomorrow + datetime.timedelta(days=6)
        if value < tomorrow:
            raise ValueError('Allocation generation is only allowed from tomorrow onwards.')
        if value > max_date:
            raise ValueError('You can only generate for the next 7 days.')
        return value

    @transaction.atomic
    def post(self, request):
        date_str = request.data.get('date')
        if not date_str:
            return Response({'detail': 'date is required (YYYY-MM-DD).'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            req_date = datetime.date.fromisoformat(date_str)
            self._validate_date_range(req_date)
        except ValueError:
            return Response({'detail': 'Invalid date format. Use YYYY-MM-DD.'}, status=status.HTTP_400_BAD_REQUEST)

        consumer = getattr(request.user, 'managed_consumer', None)
        if consumer is None:
            return Response({'detail': 'Consumer not linked for this manager user.'}, status=status.HTTP_400_BAD_REQUEST)

        run, created = ConsumerGeneratorAllocationRun.objects.get_or_create(
            consumer=consumer,
            date=req_date,
            defaults={'created_by_user': request.user, 'status': ConsumerGeneratorAllocationRun.Status.SUGGESTED},
        )
        run.status = ConsumerGeneratorAllocationRun.Status.SUGGESTED
        run.created_by_user = request.user
        run.approved_at = None
        run.save(update_fields=['status', 'created_by_user', 'approved_at'])

        # Reset overrides for this run.
        ConsumerGeneratorAllocationOverride.objects.filter(run=run).delete()

        return Response(
            {
                'date': req_date,
                'consumer_id': consumer.id,
                'generated': True,
                'run_status': run.status,
            },
            status=status.HTTP_200_OK,
        )


def _compute_slot_savings_vs_iex_for_slot(
    *,
    slot_index: int,
    time_block: str,
    unalloc: float,
    mcp_slot: float,
    plants: list,
    remaining_need_by_plant_slot: dict,
    contract_rs_per_mwh: float,
) -> dict:
    """
    Per-slot profit on the *unallocated* AI pool (45% of slot supply):

    - Headroom per plant = demand − base allocation for that slot (same as
      remaining_need_by_plant_slot): unallocated MWh cannot exceed what the plant
      can still absorb without crossing demand.
    - Sort plants by slot tariff difference (grid − RE), highest first.
    - Greedily send unallocated MWh to plants only while plant margin (Rs/MWh)
      exceeds IEX net (MCP − contract); cap each step by remaining headroom.
    - Send any remainder to IEX.
    - Choose “allocate” vs “sell all on IEX” by comparing total Rs for the mixed
      path (plants + IEX remainder) against selling 100% of unallocated on IEX.
    """
    plant_data = []
    for p in plants:
        pid = p['plant_id']
        rn = float(remaining_need_by_plant_slot.get(pid, {}).get(slot_index, 0) or 0)
        htd = p.get('hourly_tariff_difference') or []
        tariff_diff = float(tariff_diff_for_slot(htd, slot_index))
        if not htd:
            tariff_diff = float(p.get('grid_tariff_per_unit', 0) or 0) - float(p.get('re_tariff_per_unit', 0) or 0)
        plant_data.append(
            {
                'plant_id': pid,
                'plant_name': p['plant_name'],
                'tariff_diff': tariff_diff,
                'remaining_need': rn,
            }
        )

    iex_margin_rs_per_mwh = float(mcp_slot) - float(contract_rs_per_mwh)
    sell_all_rs = round(float(unalloc) * iex_margin_rs_per_mwh, 2)

    def _plant_row_base(pd: dict) -> dict:
        margin = round(float(pd['tariff_diff']) * 1000.0, 2)
        return {
            'plant_id': pd['plant_id'],
            'plant_name': pd['plant_name'],
            'savings_rs': 0.0,
            'margin_rs_per_mwh': margin,
            'margin_vs_iex_rs_per_mwh': round(margin - iex_margin_rs_per_mwh, 2),
            'remaining_headroom_mwh': round(float(pd['remaining_need']), 4),
        }

    plant_savings_slot = [_plant_row_base(pd) for pd in plant_data]
    savings_by_pid = {row['plant_id']: row for row in plant_savings_slot}

    if unalloc <= 0:
        return {
            'slot_index': slot_index,
            'time_block': time_block,
            'unallocated_mwh': round(unalloc, 4),
            'mcp_rs_per_mwh': round(float(mcp_slot), 2),
            'iex_net_rs_per_mwh': round(iex_margin_rs_per_mwh, 2),
            'sell_remainder_mwh': 0.0,
            'total_path_savings_rs': round(sell_all_rs, 2),
            'plant_savings': [dict(x) for x in plant_savings_slot],
            'allocation_split': [],
            'allocation_savings': 0.0,
            'iex_savings': round(sell_all_rs, 2),
            'best_allocate_savings_rs': 0.0,
            'sell_savings_rs': round(sell_all_rs, 2),
            'best_option': 'sell',
            'recommendation': 'No unallocated energy for this slot.',
        }

    sorted_slot = sorted(plant_data, key=lambda x: x['tariff_diff'], reverse=True)
    remaining_energy = float(unalloc)
    remaining_dem = {pd['plant_id']: pd['remaining_need'] for pd in plant_data}
    allocations_list: list[dict] = []
    alloc_by_pid: dict[int, float] = {pd['plant_id']: 0.0 for pd in plant_data}
    total_allocation_savings = 0.0

    for pd in sorted_slot:
        if remaining_energy <= 0:
            break
        pid = pd['plant_id']
        rd = remaining_dem.get(pid, 0.0)
        if rd <= 0:
            continue
        plant_rs_per_mwh = pd['tariff_diff'] * 1000.0
        if plant_rs_per_mwh <= iex_margin_rs_per_mwh:
            continue
        allocatable = min(remaining_energy, rd)
        if allocatable <= 0:
            continue
        total_allocation_savings += allocatable * plant_rs_per_mwh
        rounded_amt = round(allocatable, 2)
        allocations_list.append(
            {'plant_id': pid, 'plant_name': pd['plant_name'], 'allocated_mwh': rounded_amt}
        )
        alloc_by_pid[pid] = alloc_by_pid.get(pid, 0.0) + allocatable
        remaining_energy -= allocatable
        remaining_dem[pid] = rd - allocatable

    sell_to_iex = max(remaining_energy, 0.0)
    iex_savings_on_remainder = sell_to_iex * iex_margin_rs_per_mwh
    total_path_rs = total_allocation_savings + iex_savings_on_remainder
    total_allocation_savings = round(total_allocation_savings, 2)
    total_path_rs_rounded = round(total_path_rs, 2)

    for pd in plant_data:
        pid = pd['plant_id']
        amt = alloc_by_pid.get(pid, 0.0)
        row = savings_by_pid[pid]
        row['savings_rs'] = round(amt * pd['tariff_diff'] * 1000.0, 2)

    iex_savings_val = round(float(sell_all_rs), 2)
    alloc_sav_computed = float(total_allocation_savings)

    merged_mwh: dict[int, tuple[str, float]] = {}
    for a in allocations_list:
        pid = a['plant_id']
        nm = a['plant_name']
        mw = float(a['allocated_mwh'])
        if pid in merged_mwh:
            merged_mwh[pid] = (nm, merged_mwh[pid][1] + mw)
        else:
            merged_mwh[pid] = (nm, mw)
    allocation_split_raw = [
        {'plant_id': pid, 'plant': nm, 'mwh': round(mw, 2)} for pid, (nm, mw) in merged_mwh.items()
    ]

    # Mixed path (plants then IEX) must beat selling *all* unallocated on IEX — not
    # plant-only savings vs full IEX (fixes cases with partial plant + IEX remainder).
    first_greedy_plant = allocations_list[0]['plant_name'] if allocations_list else None
    if not allocation_split_raw:
        best_option = 'sell'
        allocation_split = []
        allocation_savings = 0.0
    elif total_path_rs_rounded + 0.01 >= float(iex_savings_val):
        best_option = 'allocate'
        allocation_split = allocation_split_raw
        allocation_savings = round(alloc_sav_computed, 2)
    else:
        best_option = 'sell'
        allocation_split = []
        allocation_savings = 0.0

    parts_s = ', '.join(f"{x['mwh']} MWh to {x['plant']}" for x in allocation_split_raw)

    if best_option == 'sell':
        rec = f"Sell all {round(unalloc, 2)} MWh on IEX (₹{iex_savings_val:,.0f})"
    elif sell_to_iex <= 1e-9:
        lead = (
            f"Maximize unallocated profit — allocate first to {first_greedy_plant} (highest slot margin vs IEX). "
            if first_greedy_plant
            else ""
        )
        rec = f"{lead}Allocate {parts_s} (₹{allocation_savings:,.0f})"
    else:
        lead = (
            f"Maximize unallocated profit — prioritize {first_greedy_plant} (highest slot margin), then other plants; "
            if first_greedy_plant
            else ""
        )
        rec = (
            f"{lead}"
            f"Allocate {parts_s}; sell {round(sell_to_iex, 2)} MWh on IEX "
            f"(total path ₹{total_path_rs_rounded:,.0f}). "
            f"Plant allocation ₹{allocation_savings:,.0f} vs selling all {round(unalloc, 2)} MWh on IEX ₹{iex_savings_val:,.0f}."
        )

    return {
        'slot_index': slot_index,
        'time_block': time_block,
        'unallocated_mwh': round(unalloc, 4),
        'mcp_rs_per_mwh': round(float(mcp_slot), 2),
        'iex_net_rs_per_mwh': round(iex_margin_rs_per_mwh, 2),
        'sell_remainder_mwh': round(sell_to_iex, 4),
        'total_path_savings_rs': total_path_rs_rounded,
        'plant_savings': [dict(x) for x in plant_savings_slot],
        'allocation_split': allocation_split,
        'allocation_savings': allocation_savings,
        'iex_savings': iex_savings_val,
        'best_allocate_savings_rs': allocation_savings,
        'sell_savings_rs': iex_savings_val,
        'best_option': best_option,
        'recommendation': rec,
    }


def _merge_slot_approval_into_analysis(slot_savings_analysis: list, consumer: Consumer, req_date: datetime.date) -> None:
    from collections import defaultdict

    qs = SlotAllocationApproval.objects.filter(consumer=consumer, date=req_date).select_related('plant')
    by_slot: dict[int, list] = defaultdict(list)
    for row in qs.order_by('plant_id'):
        by_slot[row.slot_index].append(row)
    for entry in slot_savings_analysis:
        idx = int(entry['slot_index'])
        rows = by_slot.get(idx, [])
        if not rows:
            entry['is_approved'] = False
            entry['is_manual_override'] = False
            entry['final_allocation'] = []
            entry['iex_allocation_mwh'] = 0.0
            entry['approved_revision'] = None
            continue
        entry['is_approved'] = True
        entry['approved_revision'] = rows[0].approved_revision
        entry['is_manual_override'] = any(r.is_manual_override for r in rows)
        iex_amt = sum(float(r.allocated_mwh or 0) for r in rows if r.plant_id is None)
        entry['iex_allocation_mwh'] = round(iex_amt, 4)
        plant_rows = [r for r in rows if r.plant_id is not None]
        if len(rows) == 1 and rows[0].plant_id is None and (rows[0].allocated_mwh or 0) <= 1e-9:
            entry['final_allocation'] = []
        else:
            entry['final_allocation'] = [
                {'plant': r.plant.name, 'mwh': float(r.allocated_mwh)} for r in plant_rows
            ]


class ConsumerGeneratorAllocationRecommendationsView(APIView):
    """
    Returns per-plant allocation recommendations for a consumer+date.
    If there is an approved run, applies its AI overrides to compute final totals.
    """

    permission_classes = [IsAuthenticated, IsConsumerManager]

    def _validate_date_range(self, value: datetime.date) -> datetime.date:
        today = timezone.localdate()
        max_date = today + datetime.timedelta(days=6)
        if value < today:
            raise ValueError('Allocation is only available from today onwards.')
        if value > max_date:
            raise ValueError('You can only view for the next 7 days.')
        return value

    def get(self, request):
        date_str = request.query_params.get('date')
        if not date_str:
            return Response({'detail': 'date query parameter is required.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            req_date = datetime.date.fromisoformat(date_str)
            self._validate_date_range(req_date)
        except ValueError:
            return Response({'detail': 'Invalid date format. Use YYYY-MM-DD.'}, status=status.HTTP_400_BAD_REQUEST)

        consumer = getattr(request.user, 'managed_consumer', None)
        if consumer is None:
            return Response({'detail': 'Consumer not linked for this manager user.'}, status=status.HTTP_400_BAD_REQUEST)

        expire_pending_generator_supply_upload_revisions()
        ctx = load_consumer_allocation_slot_context(consumer, req_date)
        plants = ctx['plants']
        slot_rows = ctx['slot_rows']
        slot_index_to_unallocated = ctx['slot_index_to_unallocated']
        slot_index_to_mcp = ctx['slot_index_to_mcp']
        remaining_need_by_plant_slot = ctx['remaining_need_by_plant_slot']
        mcp_map = ctx['mcp_map']
        allocation_result = ctx['allocation_result']
        run = ctx['run']
        run_status = run.status if run else None

        # Hourly savings analysis: for each hour (1-24), compare allocate-to-plant vs sell
        hourly_savings_analysis = []
        for hour_1based in range(1, 25):
            start_slot = (hour_1based - 1) * 4 + 1
            end_slot = hour_1based * 4
            slots_in_hour = list(range(start_slot, end_slot + 1))
            unallocated_hourly = sum(slot_index_to_unallocated.get(s, 0) for s in slots_in_hour)
            start_mins = (slots_in_hour[0] - 1) * 15
            end_mins = slots_in_hour[-1] * 15
            slot_labels = f'{start_mins//60:02d}:{start_mins%60:02d} - {end_mins//60:02d}:{end_mins%60:02d}'
            hour_label = f'Hour {hour_1based} ({slot_labels})'

            plant_savings = []
            remaining_need_hourly_by_plant = {}
            for p in plants:
                pid = p['plant_id']
                rn_hourly = sum(remaining_need_by_plant_slot.get(pid, {}).get(s, 0) for s in slots_in_hour)
                remaining_need_hourly_by_plant[pid] = rn_hourly
                htd = p.get('hourly_tariff_difference') or []
                tariff_diff = float(average_tariff_for_hour(htd, hour_1based))
                if not htd:
                    tariff_diff = float(p.get('grid_tariff_per_unit', 0) or 0) - float(p.get('re_tariff_per_unit', 0) or 0)
                plant_savings.append({
                    'plant_id': pid,
                    'plant_name': p['plant_name'],
                    'tariff_diff': tariff_diff,
                    'remaining_need_mwh': round(rn_hourly, 4),
                })
            sorted_by_savings = sorted(plant_savings, key=lambda x: x['tariff_diff'], reverse=True)
            remaining_to_allocate = unallocated_hourly
            allocation_breakdown = []
            best_allocate_savings_rs = 0
            for ps in sorted_by_savings:
                if remaining_to_allocate <= 0:
                    break
                alloc_to_plant = min(remaining_to_allocate, ps['remaining_need_mwh'])
                if alloc_to_plant <= 0:
                    continue
                savings_p = alloc_to_plant * ps['tariff_diff'] * 1000
                best_allocate_savings_rs += savings_p
                allocation_breakdown.append({
                    'plant_name': ps['plant_name'],
                    'allocated_mwh': round(alloc_to_plant, 2),
                    'savings_rs': round(savings_p, 2),
                })
                remaining_to_allocate -= alloc_to_plant
            excess_mwh = remaining_to_allocate  # AI pool left after capping at each plant's remaining demand

            for ps in plant_savings:
                ps['savings_rs'] = round(
                    min(unallocated_hourly, ps['remaining_need_mwh']) * ps['tariff_diff'] * 1000, 2
                )
            best_plant = max(plant_savings, key=lambda x: x['savings_rs']) if plant_savings else None

            sell_savings_rs = 0
            for slot_idx in slots_in_hour:
                unalloc = slot_index_to_unallocated.get(slot_idx, 0)
                mcp = slot_index_to_mcp.get(slot_idx, 0)
                net_per_mwh = mcp - IEX_CONTRACT_TARIFF_RS_PER_MWH
                sell_savings_rs += unalloc * net_per_mwh
            sell_savings_rs = round(sell_savings_rs, 2)

            # Sell savings on excess only (proportional to slot unallocated share within the hour)
            sell_savings_excess_only = 0
            if excess_mwh > 0 and unallocated_hourly > 0:
                for slot_idx in slots_in_hour:
                    unalloc = slot_index_to_unallocated.get(slot_idx, 0)
                    mcp = slot_index_to_mcp.get(slot_idx, 0)
                    net_per_mwh = mcp - IEX_CONTRACT_TARIFF_RS_PER_MWH
                    frac = unalloc / unallocated_hourly
                    sell_savings_excess_only += excess_mwh * frac * net_per_mwh
            sell_savings_excess_only = round(sell_savings_excess_only, 2)
            mixed_total_rs = round(best_allocate_savings_rs + sell_savings_excess_only, 2)

            if not allocation_breakdown:
                best_option = 'sell'
                recommendation = f"Sell {round(unallocated_hourly, 2)} MWh (no plant demand headroom for AI portion) (savings ₹{sell_savings_rs:,.0f})"
            elif excess_mwh > 0:
                # Some energy must leave as IEX / unallocated: compare allocate+best plants + sell remainder vs sell all
                if mixed_total_rs >= sell_savings_rs:
                    best_option = 'allocate'
                    parts = ', '.join(f"{b['allocated_mwh']} MWh to {b['plant_name']}" for b in allocation_breakdown)
                    recommendation = (
                        f"Bifurcate to plants: {parts} (₹{best_allocate_savings_rs:,.0f}); "
                        f"sell remaining {round(excess_mwh, 2)} MWh to IEX (₹{sell_savings_excess_only:,.0f}); "
                        f"mixed total ₹{mixed_total_rs:,.0f} vs sell-all ₹{sell_savings_rs:,.0f}"
                    )
                else:
                    best_option = 'sell'
                    parts_br = ', '.join(f"{b['allocated_mwh']} MWh to {b['plant_name']}" for b in allocation_breakdown)
                    recommendation = (
                        f"Sell all {round(unallocated_hourly, 2)} MWh to IEX (₹{sell_savings_rs:,.0f}) — "
                        f"better than plant split ({parts_br}) + sell remainder (₹{mixed_total_rs:,.0f})"
                    )
            elif best_allocate_savings_rs >= sell_savings_rs:
                best_option = 'allocate'
                if len(allocation_breakdown) == 1:
                    recommendation = f"Allocate {allocation_breakdown[0]['allocated_mwh']} MWh to {allocation_breakdown[0]['plant_name']} (savings ₹{best_allocate_savings_rs:,.0f})"
                else:
                    parts = ', '.join(f"{b['allocated_mwh']} MWh to {b['plant_name']}" for b in allocation_breakdown)
                    recommendation = f"Bifurcate: {parts} (savings ₹{best_allocate_savings_rs:,.0f})"
            else:
                best_option = 'sell'
                recommendation = f"Sell {round(unallocated_hourly, 2)} MWh at market (savings ₹{sell_savings_rs:,.0f})"

            hourly_savings_analysis.append({
                'hour': hour_1based,
                'hour_label': hour_label,
                'slots_included': slots_in_hour,
                'unallocated_mwh': round(unallocated_hourly, 4),
                'plant_savings': plant_savings,
                'allocation_breakdown': allocation_breakdown,
                'best_plant_id': best_plant['plant_id'] if best_plant else None,
                'best_plant_name': best_plant['plant_name'] if best_plant else None,
                'best_allocate_savings_rs': best_allocate_savings_rs,
                'sell_savings_rs': sell_savings_rs,
                'best_option': best_option,
                'recommendation': recommendation,
            })

        # Slot-level analysis: plant margin vs IEX per slot (demand-safe; bifurcate then sell remainder)
        slot_savings_analysis = []
        for s in slot_rows:
            idx = int(s['slot_index'])
            unalloc = slot_index_to_unallocated.get(idx, 0)
            mcp_slot = slot_index_to_mcp.get(idx, 0)
            slot_savings_analysis.append(
                _compute_slot_savings_vs_iex_for_slot(
                    slot_index=idx,
                    time_block=s.get('time_block', ''),
                    unalloc=unalloc,
                    mcp_slot=mcp_slot,
                    plants=plants,
                    remaining_need_by_plant_slot=remaining_need_by_plant_slot,
                    contract_rs_per_mwh=IEX_CONTRACT_TARIFF_RS_PER_MWH,
                )
            )

        _merge_slot_approval_into_analysis(slot_savings_analysis, consumer, req_date)

        slot_revision_unallocated = ctx.get('slot_revision_unallocated') or {}
        slot_default_supply_revision_key = ctx.get('slot_default_supply_revision_key') or {}
        for entry in slot_savings_analysis:
            idx = int(entry['slot_index'])
            entry['unallocated_by_supply_revision'] = dict(slot_revision_unallocated.get(idx, {}))
            entry['supply_default_revision_key'] = str(slot_default_supply_revision_key.get(idx, 'submitted'))

        generator_revision_numbers = ctx.get('generator_revision_numbers') or []
        max_upload_n = max(generator_revision_numbers) if generator_revision_numbers else 0
        generator_supply_revision_labels = [{'key': 'submitted', 'label': 'Submitted schedule'}]
        for n in generator_revision_numbers:
            generator_supply_revision_labels.append({'key': f'upload_{n}', 'label': f'Revision {n}'})
        default_supply_revision_key = f'upload_{max_upload_n}' if max_upload_n else 'submitted'

        schedule = ctx.get('generator_supply_schedule')
        pending_supply_revision = None
        if schedule:
            pend = (
                GeneratorSupplyUploadRevision.objects.filter(
                    schedule=schedule,
                    cm_review_status=GeneratorSupplyUploadRevision.CMReviewStatus.PENDING,
                )
                .prefetch_related('deltas')
                .order_by('-revision_number')
                .first()
            )
            if pend:
                now = timezone.now()
                sec_rem = (
                    max(0, int((pend.deadline_at - now).total_seconds())) if pend.deadline_at else 0
                )
                pending_supply_revision = {
                    'id': pend.id,
                    'revision_number': pend.revision_number,
                    'seconds_remaining': sec_rem,
                    'deadline_at': pend.deadline_at.isoformat() if pend.deadline_at else None,
                    'changed_slot_indices': sorted({int(d.slot_index) for d in pend.deltas.all()}),
                    'changed_slots': [
                        {
                            'slot_index': int(d.slot_index),
                            'previous_mwh': float(d.previous_mwh),
                            'new_mwh': float(d.new_mwh),
                        }
                        for d in sorted(pend.deltas.all(), key=lambda x: x.slot_index)
                    ],
                }

        plant_slot_allocations = allocation_result.get('plant_slot_allocations') or []

        return Response(
            {
                'consumer_id': consumer.id,
                'date': req_date,
                'run_status': run_status,
                'plants': plants,
                'slot_rows': [{'slot_index': s['slot_index'], 'time_block': s.get('time_block', ''), 'allocated_mwh': s['allocated_mwh']} for s in slot_rows],
                'plant_slot_allocations': plant_slot_allocations,
                'hourly_savings_analysis': hourly_savings_analysis,
                'slot_savings_analysis': slot_savings_analysis,
                'mcp_by_slot': {str(k): float(v) for k, v in (mcp_map or {}).items()},
                'generator_supply_revision_labels': generator_supply_revision_labels,
                'default_supply_revision_key': default_supply_revision_key,
                'pending_supply_revision': pending_supply_revision,
            },
            status=status.HTTP_200_OK,
        )


def _apply_slot_approval_from_context(
    consumer,
    req_date: datetime.date,
    slot_index: int,
    approved_revision: str,
    ctx: dict,
) -> dict:
    """
    Persist SlotAllocationApproval rows for one slot using a pre-loaded allocation context.
    Reused by single-slot and bulk approve so the context is loaded once per request when possible.
    """
    unalloc = resolve_unallocated_for_revision(ctx, slot_index, approved_revision)
    mcp = float(ctx['slot_index_to_mcp'].get(slot_index, 0.0))
    slot_row = next((s for s in ctx['slot_rows'] if int(s['slot_index']) == slot_index), None)
    time_block = slot_row.get('time_block', '') if slot_row else ''
    slot_dict = _compute_slot_savings_vs_iex_for_slot(
        slot_index=slot_index,
        time_block=time_block,
        unalloc=unalloc,
        mcp_slot=mcp,
        plants=ctx['plants'],
        remaining_need_by_plant_slot=ctx['remaining_need_by_plant_slot'],
        contract_rs_per_mwh=IEX_CONTRACT_TARIFF_RS_PER_MWH,
    )
    SlotAllocationApproval.objects.filter(consumer=consumer, date=req_date, slot_index=slot_index).delete()
    split = slot_dict.get('allocation_split') or []
    sell_rem = float(slot_dict.get('sell_remainder_mwh') or 0)
    unalloc_f = float(slot_dict.get('unallocated_mwh') or 0)
    if slot_dict.get('best_option') == 'allocate' and split:
        for item in split:
            pid = int(item['plant_id'])
            mwh = float(item['mwh'])
            plant = Plant.objects.filter(id=pid, consumer=consumer).first()
            if plant is None:
                raise ValueError(f'Unknown plant_id {pid}.')
            SlotAllocationApproval.objects.create(
                consumer=consumer,
                date=req_date,
                slot_index=slot_index,
                plant=plant,
                allocated_mwh=mwh,
                is_manual_override=False,
                approved_revision=approved_revision,
            )
        if sell_rem > 1e-9:
            SlotAllocationApproval.objects.create(
                consumer=consumer,
                date=req_date,
                slot_index=slot_index,
                plant=None,
                allocated_mwh=sell_rem,
                is_manual_override=False,
                approved_revision=approved_revision,
            )
    else:
        iex_mwh = unalloc_f if slot_dict.get('best_option') == 'sell' else 0.0
        SlotAllocationApproval.objects.create(
            consumer=consumer,
            date=req_date,
            slot_index=slot_index,
            plant=None,
            allocated_mwh=iex_mwh,
            is_manual_override=False,
            approved_revision=approved_revision,
        )
    sell_iex = slot_dict.get('best_option') != 'allocate' or not split
    return {
        'ok': True,
        'slot_index': slot_index,
        'date': str(req_date),
        'final_allocation': [
            {'plant_id': int(x['plant_id']), 'plant': x['plant'], 'mwh': float(x['mwh'])} for x in split
        ]
        if not sell_iex
        else [],
        'sell_to_iex_only': sell_iex,
    }


class SlotApproveView(APIView):
    permission_classes = [IsAuthenticated, IsConsumerManager]

    @transaction.atomic
    def post(self, request):
        ser = SlotApproveSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        consumer = getattr(request.user, 'managed_consumer', None)
        if consumer is None:
            return Response({'detail': 'Consumer not linked for this manager user.'}, status=status.HTTP_400_BAD_REQUEST)
        if int(ser.validated_data['consumer_id']) != int(consumer.id):
            return Response({'detail': 'consumer_id does not match your account.'}, status=status.HTTP_403_FORBIDDEN)
        req_date = ser.validated_data['date']
        slot_index = int(ser.validated_data['slot_index'])
        approved_revision = ser.validated_data.get('approved_revision') or 'revision2'
        try:
            ctx = load_consumer_allocation_slot_context(consumer, req_date)
            payload = _apply_slot_approval_from_context(consumer, req_date, slot_index, approved_revision, ctx)
        except ValueError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(payload, status=status.HTTP_200_OK)


class SlotApproveBulkView(APIView):
    """
    Approve many slots in one round-trip. Loads allocation context once, then applies each slot approval.
    """

    permission_classes = [IsAuthenticated, IsConsumerManager]

    @transaction.atomic
    def post(self, request):
        ser = SlotApproveBulkSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        consumer = getattr(request.user, 'managed_consumer', None)
        if consumer is None:
            return Response({'detail': 'Consumer not linked for this manager user.'}, status=status.HTTP_400_BAD_REQUEST)
        if int(ser.validated_data['consumer_id']) != int(consumer.id):
            return Response({'detail': 'consumer_id does not match your account.'}, status=status.HTTP_403_FORBIDDEN)
        req_date = ser.validated_data['date']
        items = ser.validated_data['slots']
        try:
            ctx = load_consumer_allocation_slot_context(consumer, req_date)
        except Exception:
            return Response({'detail': 'Failed to load allocation context for this date.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        results = []
        for item in items:
            slot_index = int(item['slot_index'])
            approved_revision = item.get('approved_revision') or 'revision2'
            try:
                results.append(
                    _apply_slot_approval_from_context(consumer, req_date, slot_index, approved_revision, ctx)
                )
            except ValueError as e:
                return Response({'detail': str(e), 'failed_at_slot': slot_index}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            {
                'ok': True,
                'date': str(req_date),
                'approved': len(results),
                'results': results,
            },
            status=status.HTTP_200_OK,
        )


class SlotRevokeView(APIView):
    permission_classes = [IsAuthenticated, IsConsumerManager]

    @transaction.atomic
    def post(self, request):
        ser = SlotRevokeSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        consumer = getattr(request.user, 'managed_consumer', None)
        if consumer is None:
            return Response({'detail': 'Consumer not linked for this manager user.'}, status=status.HTTP_400_BAD_REQUEST)
        if int(ser.validated_data['consumer_id']) != int(consumer.id):
            return Response({'detail': 'consumer_id does not match your account.'}, status=status.HTTP_403_FORBIDDEN)
        req_date = ser.validated_data['date']
        slot_index = int(ser.validated_data['slot_index'])
        deleted, _ = SlotAllocationApproval.objects.filter(
            consumer=consumer, date=req_date, slot_index=slot_index
        ).delete()
        return Response(
            {
                'ok': True,
                'slot_index': slot_index,
                'date': str(req_date),
                'deleted_rows': int(deleted),
            },
            status=status.HTTP_200_OK,
        )


class SlotOverrideApproveView(APIView):
    permission_classes = [IsAuthenticated, IsConsumerManager]

    @transaction.atomic
    def post(self, request):
        ser = SlotOverrideApproveSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        consumer = getattr(request.user, 'managed_consumer', None)
        if consumer is None:
            return Response({'detail': 'Consumer not linked for this manager user.'}, status=status.HTTP_400_BAD_REQUEST)
        if int(ser.validated_data['consumer_id']) != int(consumer.id):
            return Response({'detail': 'consumer_id does not match your account.'}, status=status.HTTP_403_FORBIDDEN)
        req_date = ser.validated_data['date']
        slot_index = int(ser.validated_data['slot_index'])
        allocations = ser.validated_data['allocations']
        approved_revision = ser.validated_data.get('approved_revision') or 'revision2'
        ctx = load_consumer_allocation_slot_context(consumer, req_date)
        unalloc_pool = resolve_unallocated_for_revision(ctx, slot_index, approved_revision)
        total_alloc = sum(float(x['mwh']) for x in allocations if float(x['mwh']) > 0)
        if total_alloc > unalloc_pool + 1e-6:
            return Response(
                {'detail': f'Total AI allocation {total_alloc} MWh exceeds unallocated pool {unalloc_pool} MWh for this slot.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        for item in allocations:
            mwh = float(item['mwh'])
            if mwh < 0:
                return Response({'detail': 'Negative allocation not allowed.'}, status=status.HTTP_400_BAD_REQUEST)
            if mwh <= 1e-9:
                continue
            pid = int(item['plant_id'])
            if not Plant.objects.filter(id=pid, consumer=consumer).exists():
                return Response({'detail': f'Unknown plant_id {pid}.'}, status=status.HTTP_400_BAD_REQUEST)
            need = float(ctx['remaining_need_by_plant_slot'].get(pid, {}).get(slot_index, 0.0))
            if mwh > need + 1e-6:
                return Response(
                    {
                        'detail': (
                            f'Plant {pid}: allocation {mwh} MWh exceeds remaining AI headroom {need} MWh for this slot.'
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        SlotAllocationApproval.objects.filter(consumer=consumer, date=req_date, slot_index=slot_index).delete()
        created = []
        remainder_iex = max(0.0, unalloc_pool - total_alloc)
        if total_alloc <= 1e-9:
            SlotAllocationApproval.objects.create(
                consumer=consumer,
                date=req_date,
                slot_index=slot_index,
                plant=None,
                allocated_mwh=unalloc_pool,
                is_manual_override=True,
                approved_revision=approved_revision,
            )
        else:
            for item in allocations:
                mwh = float(item['mwh'])
                if mwh <= 1e-9:
                    continue
                plant = Plant.objects.get(id=int(item['plant_id']), consumer=consumer)
                SlotAllocationApproval.objects.create(
                    consumer=consumer,
                    date=req_date,
                    slot_index=slot_index,
                    plant=plant,
                    allocated_mwh=mwh,
                    is_manual_override=True,
                    approved_revision=approved_revision,
                )
                created.append({'plant_id': plant.id, 'plant': plant.name, 'mwh': mwh})
            if remainder_iex > 1e-9:
                SlotAllocationApproval.objects.create(
                    consumer=consumer,
                    date=req_date,
                    slot_index=slot_index,
                    plant=None,
                    allocated_mwh=remainder_iex,
                    is_manual_override=True,
                    approved_revision=approved_revision,
                )
        return Response(
            {
                'ok': True,
                'slot_index': slot_index,
                'date': str(req_date),
                'final_allocation': created,
                'sell_to_iex_only': total_alloc <= 1e-9,
            },
            status=status.HTTP_200_OK,
        )


class ConsumerGeneratorAllocationApproveView(APIView):
    """
    Approves the allocation for a consumer+date and stores AI overrides (AI portion only).
    Overrides are day totals per plant; slot-wise allocations are re-derived deterministically.
    """

    permission_classes = [IsAuthenticated, IsConsumerManager]

    def _validate_date_range(self, value: datetime.date) -> datetime.date:
        today = timezone.localdate()
        max_date = today + datetime.timedelta(days=6)
        if value < today:
            raise ValueError('Approval is only allowed from today onwards.')
        if value > max_date:
            raise ValueError('You can only approve for the next 7 days.')
        return value

    @transaction.atomic
    def post(self, request):
        date_str = request.data.get('date')
        overrides = request.data.get('overrides', []) or []
        if not date_str:
            return Response({'detail': 'date is required (YYYY-MM-DD).'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            req_date = datetime.date.fromisoformat(date_str)
            self._validate_date_range(req_date)
        except ValueError:
            return Response({'detail': 'Invalid date format. Use YYYY-MM-DD.'}, status=status.HTTP_400_BAD_REQUEST)

        consumer = getattr(request.user, 'managed_consumer', None)
        if consumer is None:
            return Response({'detail': 'Consumer not linked for this manager user.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            mcp_map = ensure_iex_mcp_for_date(req_date)
        except Exception:
            mcp_map = None

        run, _created = ConsumerGeneratorAllocationRun.objects.get_or_create(
            consumer=consumer,
            date=req_date,
            defaults={'created_by_user': request.user, 'status': ConsumerGeneratorAllocationRun.Status.SUGGESTED},
        )
        run.status = ConsumerGeneratorAllocationRun.Status.APPROVED
        run.created_by_user = request.user
        run.approved_at = timezone.now()
        run.save(update_fields=['status', 'created_by_user', 'approved_at'])

        # Compute demand totals for clamping AI overrides to max 45% of plant demand.
        baseline = compute_allocation_with_ai_overrides(consumer, req_date, {}, mcp_by_slot_index=mcp_map)
        demand_total_by_plant_id: dict[int, Decimal] = {
            int(p['plant_id']): Decimal(p['demand_total_mwh']) for p in (baseline.get('plants') or [])
        }
        ai_max_by_plant_id: dict[int, Decimal] = {
            pid: (demand_total_by_plant_id[pid] * Decimal("0.45")) for pid in demand_total_by_plant_id
        }

        # Reset existing overrides.
        ConsumerGeneratorAllocationOverride.objects.filter(run=run).delete()

        # Normalize/insert overrides.
        plant_ids_in_consumer = set(Plant.objects.filter(consumer=consumer).values_list('id', flat=True))
        to_create: list[ConsumerGeneratorAllocationOverride] = []

        for item in overrides:
            try:
                plant_id = int(item.get('plant_id'))
                ai_override = Decimal(str(item.get('ai_alloc_mwh_override_total_mwh') or item.get('ai_alloc_mwh_override_total') or 0))
            except (TypeError, ValueError):
                continue

            if plant_id not in plant_ids_in_consumer:
                continue

            ai_max = ai_max_by_plant_id.get(plant_id, Decimal("0"))
            ai_override = max(Decimal("0"), min(ai_override, ai_max))

            to_create.append(
                ConsumerGeneratorAllocationOverride(
                    run=run,
                    plant_id=plant_id,
                    ai_alloc_mwh_override_total=ai_override,
                )
            )

        if to_create:
            ConsumerGeneratorAllocationOverride.objects.bulk_create(to_create)

        return Response(
            {
                'date': req_date,
                'consumer_id': consumer.id,
                'approved': True,
                'overrides_saved': len(to_create),
            },
            status=status.HTTP_200_OK,
        )


class ConsumerGeneratorAllocationRevokeView(APIView):
    """
    Sets the consumer+date allocation run back to SUGGESTED and clears all slot-level approvals.
    """

    permission_classes = [IsAuthenticated, IsConsumerManager]

    def _validate_date_range(self, value: datetime.date) -> datetime.date:
        today = timezone.localdate()
        max_date = today + datetime.timedelta(days=6)
        if value < today:
            raise ValueError('Revoke is only allowed from today onwards.')
        if value > max_date:
            raise ValueError('You can only revoke within the next 7 days.')
        return value

    @transaction.atomic
    def post(self, request):
        date_str = request.data.get('date')
        if not date_str:
            return Response({'detail': 'date is required (YYYY-MM-DD).'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            req_date = datetime.date.fromisoformat(date_str)
            self._validate_date_range(req_date)
        except ValueError:
            return Response({'detail': 'Invalid date format. Use YYYY-MM-DD.'}, status=status.HTTP_400_BAD_REQUEST)

        consumer = getattr(request.user, 'managed_consumer', None)
        if consumer is None:
            return Response({'detail': 'Consumer not linked for this manager user.'}, status=status.HTTP_400_BAD_REQUEST)

        run = ConsumerGeneratorAllocationRun.objects.filter(consumer=consumer, date=req_date).first()
        if run is None:
            return Response({'detail': 'No allocation run exists for this date.'}, status=status.HTTP_404_NOT_FOUND)

        run.status = ConsumerGeneratorAllocationRun.Status.SUGGESTED
        run.approved_at = None
        run.save(update_fields=['status', 'approved_at'])

        SlotAllocationApproval.objects.filter(consumer=consumer, date=req_date).delete()

        return Response(
            {
                'date': str(req_date),
                'consumer_id': consumer.id,
                'revoked': True,
                'run_status': run.status,
            },
            status=status.HTTP_200_OK,
        )


class IexGreenDayAheadMcpView(APIView):
    """
    GET: returns cached MCP for a delivery date (and attempts live fetch if missing).
    POST: manual upsert of MCP for 96 slots.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        date_str = request.query_params.get('date')
        if not date_str:
            return Response({'detail': 'date query parameter is required (YYYY-MM-DD).'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            req_date = datetime.date.fromisoformat(date_str)
        except ValueError:
            return Response({'detail': 'Invalid date format. Use YYYY-MM-DD.'}, status=status.HTTP_400_BAD_REQUEST)

        existing_qs = IexGreenDayAheadMcpSlot.objects.filter(date=req_date)
        if existing_qs.count() >= SLOTS_PER_DAY:
            slots = [
                {
                    'slot_index': s.slot_index,
                    'slot_time': s.slot_time.strftime('%H:%M'),
                    'time_block': slot_index_to_time_block(int(s.slot_index)),
                    'mcp_rs_per_mwh': str(s.mcp_rs_per_mwh),
                }
                for s in existing_qs.order_by('slot_index')
            ]
            return Response({'date': req_date, 'slots': slots}, status=status.HTTP_200_OK)

        # Not enough cached slots: fetch or predict via ensure_iex_mcp_for_date.
        from allocation.iex_service import ensure_iex_mcp_for_date

        ensure_iex_mcp_for_date(req_date)

        slots = [
            {
                'slot_index': s.slot_index,
                'slot_time': s.slot_time.strftime('%H:%M'),
                'time_block': slot_index_to_time_block(int(s.slot_index)),
                'mcp_rs_per_mwh': str(s.mcp_rs_per_mwh),
            }
            for s in IexGreenDayAheadMcpSlot.objects.filter(date=req_date).order_by('slot_index')
        ]
        return Response({'date': req_date, 'slots': slots}, status=status.HTTP_200_OK)

    def post(self, request):
        serializer = IexMcpUpsertSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        req_date = data['date']
        expected_slots = generate_day_slots()
        slot_time_by_index = {int(s['slot_index']): s['slot_time'] for s in expected_slots}

        IexGreenDayAheadMcpSlot.objects.filter(date=req_date).delete()
        bulk = []
        for slot in data['slots']:
            idx = int(slot['slot_index'])
            bulk.append(
                IexGreenDayAheadMcpSlot(
                    date=req_date,
                    slot_index=idx,
                    slot_time=slot_time_by_index[idx],
                    mcp_rs_per_mwh=slot['mcp_rs_per_mwh'],
                )
            )
        IexGreenDayAheadMcpSlot.objects.bulk_create(bulk)

        slots = [
            {
                'slot_index': s.slot_index,
                'slot_time': s.slot_time.strftime('%H:%M'),
                'time_block': slot_index_to_time_block(int(s.slot_index)),
                'mcp_rs_per_mwh': str(s.mcp_rs_per_mwh),
            }
            for s in IexGreenDayAheadMcpSlot.objects.filter(date=req_date).order_by('slot_index')
        ]
        return Response({'date': req_date, 'slots': slots}, status=status.HTTP_200_OK)


class IexPredictionCompareView(APIView):
    """Predicted vs Actual MCP comparison. Today: actual from IEX + directional accuracy. Tomorrow: prediction only."""
    permission_classes = [IsAuthenticated]

    def _price_level(self, mcp_rs_per_mwh: float) -> str:
        if mcp_rs_per_mwh < 4700:
            return 'LOW'
        if mcp_rs_per_mwh < 5200:
            return 'MEDIUM'
        return 'HIGH'

    def get(self, request):
        date_str = request.query_params.get('date')
        if not date_str:
            return Response({'detail': 'date required (YYYY-MM-DD).'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            req_date = datetime.date.fromisoformat(date_str)
        except ValueError:
            return Response({'detail': 'Invalid date.'}, status=status.HTTP_400_BAD_REQUEST)

        from allocation.iex_prediction import IexMcpPredictor
        from pathlib import Path
        model_path = Path(__file__).resolve().parent.parent / "data" / "iex_mcp_model.pkl"
        predictor = IexMcpPredictor(model_path=model_path)
        predicted = predictor.predict(req_date)

        actual_by_slot = None
        today = timezone.localdate()
        if req_date <= today:
            # Try live IEX fetch first (only works when IEX page shows requested date, i.e. usually today)
            try:
                delivery_date, actual_map = fetch_iex_green_day_ahead_mcp(req_date)
                if delivery_date == req_date and actual_map and len(actual_map) >= SLOTS_PER_DAY:
                    nonzero = sum(1 for v in actual_map.values() if v and float(v) > 0)
                    if nonzero >= 48:  # At least half non-zero = good data
                        actual_by_slot = actual_map
            except Exception:
                pass
            # For past dates or when live fetch fails, use cached IexGreenDayAheadMcpSlot
            if actual_by_slot is None:
                cached = IexGreenDayAheadMcpSlot.objects.filter(date=req_date).order_by('slot_index')
                if cached.count() >= SLOTS_PER_DAY:
                    actual_map_cached = {int(s.slot_index): s.mcp_rs_per_mwh for s in cached}
                    nonzero = sum(1 for v in actual_map_cached.values() if v and float(v) > 0)
                    if nonzero >= 48:
                        actual_by_slot = actual_map_cached

        expected_slots = generate_day_slots()
        slot_time_block_by_index = {int(s['slot_index']): s['time_block'] for s in expected_slots}
        slots = []
        pred_vals = []
        for idx in range(1, SLOTS_PER_DAY + 1):
            pred_val = float(predicted.get(idx, 0))
            actual_val = float(actual_by_slot.get(idx, 0)) if actual_by_slot else None
            pred_vals.append(pred_val)
            slots.append({
                'slot_index': idx,
                'time_block': slot_time_block_by_index[idx],
                'predicted_mcp_rs_per_kwh': round(pred_val / 1000, 2),
                'actual_mcp_rs_per_kwh': round(actual_val / 1000, 2) if actual_val is not None else None,
                'price_level': self._price_level(pred_val),
                'confidence': 'HIGH',
            })

        directional_accuracy_pct = None
        if actual_by_slot and len(pred_vals) >= 2:
            matches = total = 0
            for i in range(1, SLOTS_PER_DAY):
                pred_dir = 1 if pred_vals[i] > pred_vals[i - 1] else (-1 if pred_vals[i] < pred_vals[i - 1] else 0)
                act_val_i = float(actual_by_slot.get(i + 1, 0))
                act_val_prev = float(actual_by_slot.get(i, 0))
                act_dir = 1 if act_val_i > act_val_prev else (-1 if act_val_i < act_val_prev else 0)
                if pred_dir != 0 or act_dir != 0:
                    total += 1
                    if pred_dir == act_dir:
                        matches += 1
            if total > 0:
                directional_accuracy_pct = round(100 * matches / total, 1)

        return Response({
            'date': req_date.isoformat(),
            'slots': slots,
            'has_actual': actual_by_slot is not None,
            'directional_accuracy_pct': directional_accuracy_pct,
        }, status=status.HTTP_200_OK)


class IexScrapedPredictionView(APIView):
    """
    MCP predictions scraped from https://viewmetric.in/iex-predictor/ (Consumer Manager).
    Matches the public table on that page; breaks if their HTML changes.
    """

    permission_classes = [IsAuthenticated, IsConsumerManager]

    def get(self, request):
        raw_period = (request.GET.get("delivery_period") or "yesterday").strip().lower()
        delivery_period = raw_period if raw_period in ALLOWED_DELIVERY_PERIODS else "yesterday"
        try:
            rows = fetch_iex_predictions(delivery_period=delivery_period)
        except requests.RequestException as exc:
            return Response(
                {'detail': f'Could not reach ViewMetric: {exc}'},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        except Exception as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        slots = []
        for i, r in enumerate(rows):
            block = r.get('block')
            if block is not None and 1 <= int(block) <= SLOTS_PER_DAY:
                slot_index = int(block)
            else:
                slot_index = i + 1
            pred = float(r['predicted_mcp'])
            actual = r.get('actual_mcp')
            slots.append(
                {
                    'slot_index': slot_index,
                    'time_block': r.get('time_block') or '',
                    'predicted_mcp_rs_per_kwh': round(pred, 2),
                    'actual_mcp_rs_per_kwh': round(float(actual), 2) if actual is not None else None,
                    'price_level': r.get('price_level') or '—',
                    'confidence': r.get('confidence') or '—',
                }
            )

        has_actual = any(s['actual_mcp_rs_per_kwh'] is not None for s in slots)

        return Response(
            {
                'source': f'{VIEWMETRIC_IEX_BASE}?interval=15min&delivery_period={delivery_period}&apply=true',
                'delivery_period': delivery_period,
                'slots': slots,
                'has_actual': has_actual,
                'directional_accuracy_pct': None,
            },
            status=status.HTTP_200_OK,
        )


def _tomorrow_kpis_admin():
    """Next calendar day: platform demand vs generator supply (same units as admin series)."""
    tomorrow = timezone.localdate() + datetime.timedelta(days=1)
    td = DemandSlot.objects.filter(schedule__date=tomorrow, schedule__shutdown=False).aggregate(
        s=Sum('demand_mw')
    )['s'] or Decimal('0')
    ta = GeneratorSupplySlot.objects.filter(schedule__date=tomorrow).aggregate(s=Sum('supply_mwh'))['s'] or Decimal('0')
    diff_pct = '0'
    if td > 0:
        diff_pct = str(round(float((td - ta) / td * 100), 1))
    return {
        'tomorrow_demand_mwh': str(round(td, 2)),
        'tomorrow_approved_supply_mwh': str(round(ta, 2)),
        'tomorrow_diff_pct': diff_pct,
    }


def _tomorrow_kpis_consumer_plants(consumer: Consumer):
    """Next calendar day: summed plant demand vs approved allocation (after approval)."""
    tomorrow = timezone.localdate() + datetime.timedelta(days=1)
    td = Decimal('0')
    ta = Decimal('0')
    try:
        run = ConsumerGeneratorAllocationRun.objects.filter(consumer=consumer, date=tomorrow).first()
        if run and run.status == ConsumerGeneratorAllocationRun.Status.APPROVED:
            result = compute_allocation_with_ai_overrides(consumer, tomorrow)
            for p in result.get('plants', []):
                td += Decimal(str(p.get('demand_total_mwh') or 0))
                ta += Decimal(str(p.get('allocated_total_mwh') or 0))
    except Exception:
        pass
    diff_pct = '0'
    if td > 0:
        diff_pct = str(round(float((td - ta) / td * 100), 1))
    return {
        'tomorrow_demand_mwh': str(round(td, 2)),
        'tomorrow_approved_supply_mwh': str(round(ta, 2)),
        'tomorrow_diff_pct': diff_pct,
    }


def _tomorrow_kpis_single_plant(plant: Plant, consumer: Consumer):
    """Next calendar day: one plant demand vs approved allocation."""
    tomorrow = timezone.localdate() + datetime.timedelta(days=1)
    td = Decimal('0')
    ta = Decimal('0')
    try:
        run = ConsumerGeneratorAllocationRun.objects.filter(consumer=consumer, date=tomorrow).first()
        if run and run.status == ConsumerGeneratorAllocationRun.Status.APPROVED:
            result = compute_allocation_with_ai_overrides(consumer, tomorrow)
            for p in result.get('plants', []):
                if int(p.get('plant_id') or 0) == int(plant.id):
                    td = Decimal(str(p.get('demand_total_mwh') or 0))
                    ta = Decimal(str(p.get('allocated_total_mwh') or 0))
                    break
    except Exception:
        pass
    diff_pct = '0'
    if td > 0:
        diff_pct = str(round(float((td - ta) / td * 100), 1))
    return {
        'tomorrow_demand_mwh': str(round(td, 2)),
        'tomorrow_approved_supply_mwh': str(round(ta, 2)),
        'tomorrow_diff_pct': diff_pct,
    }


class ReportsSummaryView(APIView):
    """
    Role-based report aggregates: KPIs, weekly savings trend, allocation split pie.
    - PLATFORM_ADMIN: platform-wide demand vs generator supply (full scope).
    - CONSUMER_MANAGER: consumer allocation + savings from allocator plants.
    - PLANT_USER: single-plant demand, allocation, savings.

    Query: optional ``from`` and ``to`` (YYYY-MM-DD, inclusive). When both are set,
    the series and KPIs cover only that range (max 367 days). Otherwise defaults to
    the next 7 days starting tomorrow.
    """

    permission_classes = [IsAuthenticated]

    def _report_date_list(self, request):
        today = timezone.localdate()
        from_s = request.GET.get('from')
        to_s = request.GET.get('to')
        if from_s and to_s:
            try:
                from_d = datetime.date.fromisoformat(from_s)
                to_d = datetime.date.fromisoformat(to_s)
            except ValueError:
                return None, {'detail': 'Invalid from or to date. Use YYYY-MM-DD.'}
            if from_d > to_d:
                return None, {'detail': 'from must be on or before to.'}
            if (to_d - from_d).days > 366:
                return None, {'detail': 'Date range cannot exceed 367 days.'}
            dates = []
            cur = from_d
            while cur <= to_d:
                dates.append(cur)
                cur += datetime.timedelta(days=1)
            return dates, None
        tomorrow = today + datetime.timedelta(days=1)
        dates = [tomorrow + datetime.timedelta(days=i) for i in range(7)]
        return dates, None

    def get(self, request):
        role = getattr(request.user, 'role', None)
        dates, err = self._report_date_list(request)
        if err is not None:
            return Response(err, status=status.HTTP_400_BAD_REQUEST)

        if role == 'PLATFORM_ADMIN':
            return Response(self._admin_summary(dates), status=status.HTTP_200_OK)
        if role == 'CONSUMER_MANAGER':
            data = self._consumer_manager_summary(request, dates)
            if 'detail' in data:
                return Response(data, status=status.HTTP_400_BAD_REQUEST)
            return Response(data, status=status.HTTP_200_OK)
        if role == 'PLANT_USER':
            return Response(self._plant_reports_summary(request, dates), status=status.HTTP_200_OK)
        if role == 'GENERATOR':
            return Response(self._generator_summary(request, dates), status=status.HTTP_200_OK)
        return Response(
            {'detail': 'Reports summary is available for Platform Admin, Consumer Manager, Plant User, and Generator.'},
            status=status.HTTP_403_FORBIDDEN,
        )

    def _admin_summary(self, dates):
        consumers_count = Consumer.objects.count()
        plants_count = Plant.objects.count()
        series = []
        total_demand_week = Decimal('0')
        for d in dates:
            td = DemandSlot.objects.filter(schedule__date=d, schedule__shutdown=False).aggregate(
                s=Sum('demand_mw')
            )['s'] or Decimal('0')
            total_demand_week += td
            series.append(
                {
                    'date': d.isoformat(),
                    'date_label': d.strftime('%a %d %b'),
                    'savings_rs': '0',
                    'demand_mwh': str(round(td, 4)),
                }
            )
        d0 = dates[0]
        demand_d0 = DemandSlot.objects.filter(schedule__date=d0, schedule__shutdown=False).aggregate(
            s=Sum('demand_mw')
        )['s'] or Decimal('0')
        supply_d0 = GeneratorSupplySlot.objects.filter(schedule__date=d0).aggregate(s=Sum('supply_mwh'))['s'] or Decimal('0')
        supply_capped = min(supply_d0, demand_d0) if demand_d0 > 0 else Decimal('0')
        gap = max(demand_d0 - supply_d0, Decimal('0'))
        pie = {
            'labels': ['Demand (MWh)', 'Generator supply (MWh)', 'Gap / IEX proxy (MWh)'],
            'values': [float(demand_d0), float(supply_capped), float(gap)],
        }
        iex_pct = '0'
        if demand_d0 > 0:
            iex_pct = str(round(float(gap / demand_d0 * 100), 1))

        kpis = {
            'savings_rs': '0',
            'energy_mwh': str(round(total_demand_week, 2)),
            'iex_sold_pct': iex_pct,
            'subtitle': f'{consumers_count} consumers · {plants_count} plants (platform)',
        }
        kpis.update(_tomorrow_kpis_admin())

        return {
            'scope': 'admin',
            'kpis': kpis,
            'series': series,
            'pie': pie,
        }

    def _consumer_manager_summary(self, request, dates):
        consumer = get_managed_consumer(request.user)
        if consumer is None:
            return {'detail': 'Consumer not linked for this manager user.'}

        series = []
        for d in dates:
            sav = Decimal('0')
            dem = Decimal('0')
            alloc = Decimal('0')
            gen_sup = Decimal('0')
            iex_tot = Decimal('0')
            approved_acc = Decimal('0')
            try:
                run = ConsumerGeneratorAllocationRun.objects.filter(consumer=consumer, date=d).first()
                if run and run.status == ConsumerGeneratorAllocationRun.Status.APPROVED:
                    result = compute_allocation_with_ai_overrides(consumer, d)
                    dem = sum((Decimal(str(s['demand_mwh'])) for s in result['slot_rows']), Decimal('0'))
                    alloc = sum((Decimal(str(s['allocated_mwh'])) for s in result['slot_rows']), Decimal('0'))
                    for p in result.get('plants', []):
                        sav += Decimal(str(p.get('savings_estimate') or 0))
                    approved_acc, gen_sup, iex_tot = generator_approved_allocation_day_totals(consumer, d, None)
            except Exception:
                pass
            series.append(
                {
                    'date': d.isoformat(),
                    'date_label': d.strftime('%a %d %b'),
                    'savings_rs': str(round(sav, 2)),
                    'demand_mwh': str(round(dem, 4)),
                    'allocated_mwh': str(round(alloc, 4)),
                    'generator_supply_mwh': str(round(gen_sup, 4)),
                    'approved_accounted_mwh': str(round(approved_acc, 4)),
                    'iex_mwh': str(round(iex_tot, 4)),
                }
            )

        # KPIs and pie from the same series totals as charts/tables (not day-0 only).
        total_dem = Decimal('0')
        total_alloc = Decimal('0')
        total_sav = Decimal('0')
        for row in series:
            total_dem += Decimal(str(row['demand_mwh']))
            total_alloc += Decimal(str(row['allocated_mwh']))
            total_sav += Decimal(str(row['savings_rs']))
        unmet_total = max(total_dem - total_alloc, Decimal('0'))
        pie = {
            'labels': ['Allocated (MWh)', 'Unmet demand (MWh)'],
            'values': [float(total_alloc), float(unmet_total)],
        }
        iex_pct = '0'
        if total_dem > 0:
            iex_pct = str(round(float(unmet_total / total_dem * 100), 1))
        kpis = {
            'savings_rs': str(round(total_sav, 2)),
            'energy_mwh': str(round(total_dem, 2)),
            'allocated_mwh': str(round(total_alloc, 2)),
            'iex_sold_pct': iex_pct,
            'subtitle': consumer.name,
        }
        kpis.update(_tomorrow_kpis_consumer_plants(consumer))

        return {
            'scope': 'consumer',
            'kpis': kpis,
            'series': series,
            'pie': pie,
            'demand_only': True,
        }

    def _plant_user_summary(self, request, dates):
        plant = get_plant_for_plant_user(request.user)
        consumer = plant.consumer
        series = []
        for d in dates:
            sav = Decimal('0')
            dem = Decimal('0')
            alloc = Decimal('0')
            try:
                run = ConsumerGeneratorAllocationRun.objects.filter(consumer=consumer, date=d).first()
                if run and run.status == ConsumerGeneratorAllocationRun.Status.APPROVED:
                    result = compute_allocation_with_ai_overrides(consumer, d)
                    for p in result.get('plants', []):
                        if int(p.get('plant_id') or 0) == int(plant.id):
                            sav = Decimal(str(p.get('savings_estimate') or 0))
                            dem = Decimal(str(p.get('demand_total_mwh') or 0))
                            alloc = Decimal(str(p.get('allocated_total_mwh') or 0))
                            break
            except Exception:
                pass
            series.append(
                {
                    'date': d.isoformat(),
                    'date_label': d.strftime('%a %d %b'),
                    'savings_rs': str(round(sav, 2)),
                    'demand_mwh': str(round(dem, 4)),
                    'allocated_mwh': str(round(alloc, 4)),
                }
            )

        total_dem = Decimal('0')
        total_alloc = Decimal('0')
        total_sav = Decimal('0')
        for row in series:
            total_dem += Decimal(str(row['demand_mwh']))
            total_alloc += Decimal(str(row['allocated_mwh']))
            total_sav += Decimal(str(row['savings_rs']))
        unmet_total = max(total_dem - total_alloc, Decimal('0'))
        pie = {
            'labels': ['Allocated (MWh)', 'Unmet demand (MWh)'],
            'values': [float(total_alloc), float(unmet_total)],
        }
        iex_pct = '0'
        if total_dem > 0:
            iex_pct = str(round(float(unmet_total / total_dem * 100), 1))
        kpis = {
            'savings_rs': str(round(total_sav, 2)),
            'energy_mwh': str(round(total_dem, 2)),
            'allocated_mwh': str(round(total_alloc, 2)),
            'iex_sold_pct': iex_pct,
            'subtitle': plant.name,
        }
        kpis.update(_tomorrow_kpis_single_plant(plant, consumer))

        return {
            'scope': 'plant',
            'kpis': kpis,
            'series': series,
            'pie': pie,
            'demand_only': False,
        }

    def _plant_reports_summary(self, request, dates):
        """
        Same shape as generator reports allocation series, scoped to the plant user's plant:
        daily approved gross (this plant) vs demand entry gross (this plant). No IEX fields.
        """
        plant = get_plant_for_plant_user(request.user)
        consumer = plant.consumer
        cm = consumer.consumer_manager
        series = []
        for d in dates:
            appr_plant = generator_plant_approved_gross_mwh(consumer, d, plant.id)
            dem_entry = plant_demand_gross_mwh_total(consumer, d, plant.id)
            series.append(
                {
                    'date': d.isoformat(),
                    'date_label': d.strftime('%a %d %b'),
                    'demand_mwh': str(round(dem_entry, 4)),
                    'allocated_mwh': str(round(appr_plant, 4)),
                    'supply_mwh': str(round(dem_entry, 4)),
                    'approved_allocation_mwh': str(round(appr_plant, 4)),
                    'demand_entry_mwh': str(round(dem_entry, 4)),
                }
            )

        total_demand_entry = sum(Decimal(str(row['demand_entry_mwh'])) for row in series)
        total_approved = sum(Decimal(str(row['approved_allocation_mwh'])) for row in series)
        unmet = max(total_demand_entry - total_approved, Decimal('0'))
        met = min(total_approved, total_demand_entry)
        pie = {
            'labels': ['Covered demand (MWh)', 'Unmet demand (MWh)'],
            'values': [float(met), float(unmet)],
        }
        iex_pct = '0'
        if total_demand_entry > 0:
            iex_pct = str(round(float(unmet / total_demand_entry * 100), 1))
        kpis = {
            'savings_rs': '0',
            'energy_mwh': str(round(total_demand_entry, 2)),
            'allocated_mwh': str(round(total_approved, 2)),
            'iex_sold_pct': iex_pct,
            'subtitle': plant.name,
        }
        meta = {
            'consumer_manager_user_id': int(cm.id) if cm else None,
            'plant_id': plant.id,
            'plant_name': plant.name,
        }
        return {
            'scope': 'plant_reports',
            'kpis': kpis,
            'series': series,
            'pie': pie,
            'demand_only': True,
            'plant_reports_meta': meta,
        }

    def _generator_summary(self, request, dates):
        """
        Supply section: all submitted generator supply for the day (any consumer).
        Allocation section: same approved-allocation math as GeneratorApprovedAllocationSlotsView
        (plant gross + slot approvals), paired with generator supply for schedules submitted by this user.
        """
        generator_user = request.user
        consumers = list(Consumer.objects.all())
        series = []
        for d in dates:
            supply_total_all = (
                GeneratorSupplySlot.objects.filter(schedule__date=d).aggregate(s=Sum('supply_mwh'))['s'] or Decimal('0')
            )
            approved_total = Decimal('0')
            gen_supply_pair = Decimal('0')
            iex_total_all = Decimal('0')
            for consumer in consumers:
                a, s, ix = generator_approved_allocation_day_totals(consumer, d, generator_user)
                approved_total += a
                gen_supply_pair += s
                iex_total_all += ix

            series.append(
                {
                    'date': d.isoformat(),
                    'date_label': d.strftime('%a %d %b'),
                    'demand_mwh': str(round(approved_total, 4)),
                    'allocated_mwh': str(round(approved_total, 4)),
                    'supply_mwh': str(round(supply_total_all, 4)),
                    'approved_allocation_mwh': str(round(approved_total, 4)),
                    'generator_supply_mwh': str(round(gen_supply_pair, 4)),
                    'iex_mwh': str(round(iex_total_all, 4)),
                }
            )

        total_supply = sum(Decimal(str(row['supply_mwh'])) for row in series)
        total_approved = sum(Decimal(str(row['approved_allocation_mwh'])) for row in series)
        total_gen_supply = sum(Decimal(str(row['generator_supply_mwh'])) for row in series)
        supply_gap = max(total_approved - total_gen_supply, Decimal('0'))
        pie = {
            'labels': ['Approved allocation (MWh)', 'Supply gap (MWh)'],
            'values': [float(total_approved - supply_gap), float(supply_gap)],
        }
        iex_pct = '0'
        if total_approved > 0:
            iex_pct = str(round(float(supply_gap / total_approved * 100), 1))
        kpis = {
            'savings_rs': '0',
            'energy_mwh': str(round(total_supply, 2)),
            'allocated_mwh': str(round(total_approved, 2)),
            'iex_sold_pct': iex_pct,
            'subtitle': 'Generator · all consumers',
        }

        return {
            'scope': 'generator',
            'kpis': kpis,
            'series': series,
            'pie': pie,
            'demand_only': True,
        }

