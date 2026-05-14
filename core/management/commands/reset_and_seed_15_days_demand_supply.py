"""
Remove generator supply, demand schedules, and related allocation rows for the target consumer(s), then
create fresh data for the next N days (default 15: tomorrow through tomorrow+14) for every plant under
those consumers.

By default only rows for ``--consumer-name`` are removed (other consumers are untouched). Use
``--global-wipe`` to clear those tables for the entire database (destructive).

- Deletes (scoped): slot allocation approvals, AI overrides/runs, generator supply schedules, demand
  schedules, consumer demand approvals, generator schedule approvals.
- Creates: per-plant demand (96 slots × MW) and per-consumer generator supply (96 slots × MWh) per day.

Usage:
  python manage.py reset_and_seed_15_days_demand_supply
  python manage.py reset_and_seed_15_days_demand_supply --consumer-name "Example Consumer" --days 15
  python manage.py reset_and_seed_15_days_demand_supply --all-consumers
  python manage.py reset_and_seed_15_days_demand_supply --global-wipe --consumer-name "Example Consumer"
"""

import datetime
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from accounts.models import CustomUser
from allocation.models import (
    ConsumerDemandApproval,
    ConsumerGeneratorAllocationOverride,
    ConsumerGeneratorAllocationRun,
    DemandSchedule,
    DemandSlot,
    GeneratorScheduleApproval,
    GeneratorSupplySchedule,
    GeneratorSupplySlot,
    SlotAllocationApproval,
)
from allocation.slot_utils import generate_day_slots
from core.models import Consumer, Plant, PlantUser


def _delete_consumer_scoped(consumer: Consumer) -> dict:
    """Remove scheduling/allocation data for one consumer. Returns deleted row counts (best-effort)."""
    out = {}
    qs_sa = SlotAllocationApproval.objects.filter(consumer=consumer)
    out["slot_allocation_approval"] = qs_sa.count()
    qs_sa.delete()

    qs_ov = ConsumerGeneratorAllocationOverride.objects.filter(run__consumer=consumer)
    out["allocation_override"] = qs_ov.count()
    qs_ov.delete()

    qs_run = ConsumerGeneratorAllocationRun.objects.filter(consumer=consumer)
    out["allocation_run"] = qs_run.count()
    qs_run.delete()

    qs_gs = GeneratorSupplySchedule.objects.filter(consumer=consumer)
    out["generator_supply_schedule"] = qs_gs.count()
    qs_gs.delete()

    qs_ds = DemandSchedule.objects.filter(plant__consumer=consumer)
    out["demand_schedule"] = qs_ds.count()
    qs_ds.delete()

    qs_cda = ConsumerDemandApproval.objects.filter(consumer=consumer)
    out["consumer_demand_approval"] = qs_cda.count()
    qs_cda.delete()

    qs_gsa = GeneratorScheduleApproval.objects.filter(consumer=consumer)
    out["generator_schedule_approval"] = qs_gsa.count()
    qs_gsa.delete()
    return out


def _delete_global() -> dict:
    before = {
        "slot_allocation_approval": SlotAllocationApproval.objects.count(),
        "allocation_override": ConsumerGeneratorAllocationOverride.objects.count(),
        "allocation_run": ConsumerGeneratorAllocationRun.objects.count(),
        "generator_supply_schedule": GeneratorSupplySchedule.objects.count(),
        "demand_schedule": DemandSchedule.objects.count(),
        "consumer_demand_approval": ConsumerDemandApproval.objects.count(),
        "generator_schedule_approval": GeneratorScheduleApproval.objects.count(),
    }
    SlotAllocationApproval.objects.all().delete()
    ConsumerGeneratorAllocationOverride.objects.all().delete()
    ConsumerGeneratorAllocationRun.objects.all().delete()
    GeneratorSupplySchedule.objects.all().delete()
    DemandSchedule.objects.all().delete()
    ConsumerDemandApproval.objects.all().delete()
    GeneratorScheduleApproval.objects.all().delete()
    return before


class Command(BaseCommand):
    help = (
        "Delete all demand/supply/allocation scheduling data, then seed demand + generator supply "
        "for all plants (next N days, default 15)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--consumer-name",
            default="Example Consumer",
            help="Consumer whose plants get demand + supply (default: Example Consumer).",
        )
        parser.add_argument(
            "--days",
            type=int,
            default=15,
            help="Number of calendar days starting tomorrow (default 15).",
        )
        parser.add_argument(
            "--demand-mw",
            type=float,
            default=10.0,
            help="Demand (MW) per 15-min slot for every plant (default 10).",
        )
        parser.add_argument(
            "--supply-mwh",
            type=float,
            default=16.12,
            help="Generator supply (MWh) per 15-min slot for the consumer/day (default 16.12).",
        )
        parser.add_argument(
            "--all-consumers",
            action="store_true",
            help="Seed every consumer that has at least one plant (uses scoped delete per consumer).",
        )
        parser.add_argument(
            "--global-wipe",
            action="store_true",
            help="Delete ALL demand/supply/allocation scheduling rows in the DB before seeding (dangerous).",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        consumer_name = (options.get("consumer_name") or "").strip()
        days = max(1, int(options.get("days") or 15))
        demand_mw = Decimal(str(options["demand_mw"]))
        supply_mwh = Decimal(str(options["supply_mwh"]))
        all_consumers = bool(options.get("all_consumers"))
        global_wipe = bool(options.get("global_wipe"))

        if global_wipe:
            before = _delete_global()
            self.stdout.write(
                self.style.WARNING(
                    "Global wipe — removed: "
                    f"approvals {before['slot_allocation_approval']}, overrides {before['allocation_override']}, "
                    f"runs {before['allocation_run']}, supply schedules {before['generator_supply_schedule']}, "
                    f"demand schedules {before['demand_schedule']}, demand approvals {before['consumer_demand_approval']}, "
                    f"gen schedule approvals {before['generator_schedule_approval']}."
                )
            )
        elif all_consumers:
            consumers_to_seed = list(Consumer.objects.filter(plants__isnull=False).distinct().order_by("id"))
            if not consumers_to_seed:
                self.stderr.write(self.style.ERROR("No consumers with plants found."))
                return
            for c in consumers_to_seed:
                before = _delete_consumer_scoped(c)
                self.stdout.write(
                    self.style.WARNING(
                        f'Cleared consumer {c.name!r}: slot_appr {before["slot_allocation_approval"]}, '
                        f'overrides {before["allocation_override"]}, runs {before["allocation_run"]}, '
                        f'supply {before["generator_supply_schedule"]}, demand {before["demand_schedule"]}, '
                        f'demand_appr {before["consumer_demand_approval"]}, gen_appr {before["generator_schedule_approval"]}.'
                    )
                )
        else:
            consumer_one = Consumer.objects.filter(name__iexact=consumer_name).first()
            if not consumer_one:
                self.stderr.write(self.style.ERROR(f'Consumer "{consumer_name}" not found.'))
                return
            before = _delete_consumer_scoped(consumer_one)
            self.stdout.write(
                self.style.WARNING(
                    f'Cleared consumer {consumer_one.name!r}: slot_appr {before["slot_allocation_approval"]}, '
                    f'overrides {before["allocation_override"]}, runs {before["allocation_run"]}, '
                    f'supply {before["generator_supply_schedule"]}, demand {before["demand_schedule"]}, '
                    f'demand_appr {before["consumer_demand_approval"]}, gen_appr {before["generator_schedule_approval"]}.'
                )
            )

        if all_consumers:
            consumers = list(Consumer.objects.filter(plants__isnull=False).distinct().order_by("id"))
        else:
            consumer = Consumer.objects.filter(name__iexact=consumer_name).first()
            if not consumer:
                self.stderr.write(self.style.ERROR(f'Consumer "{consumer_name}" not found. Nothing seeded.'))
                return
            consumers = [consumer]

        generator = CustomUser.objects.filter(role=CustomUser.Role.GENERATOR, is_active=True).first()
        if not generator:
            self.stderr.write(
                self.style.ERROR("No active Generator user. Create one (e.g. seed_platform_demo) before seeding supply.")
            )
            return

        def creator_for_plant(plant: Plant, cons: Consumer):
            pu = PlantUser.objects.filter(plant=plant).select_related("user").first()
            if pu:
                return pu.user
            cm = cons.consumer_manager
            if cm:
                return cm
            u = CustomUser.objects.filter(is_active=True).first()
            if not u:
                raise RuntimeError("No user available for DemandSchedule.created_by_user")
            return u

        today = timezone.localdate()
        dates = [today + datetime.timedelta(days=i) for i in range(1, days + 1)]

        for consumer in consumers:
            plants = list(Plant.objects.filter(consumer=consumer).order_by("id"))
            if not plants:
                self.stdout.write(self.style.WARNING(f"Skipping consumer {consumer.name!r} (no plants)."))
                continue

            for req_date in dates:
                for plant in plants:
                    creator = creator_for_plant(plant, consumer)
                    schedule, _ = DemandSchedule.objects.update_or_create(
                        plant=plant,
                        date=req_date,
                        defaults={
                            "shutdown": False,
                            "created_by_user": creator,
                            "updated_at": timezone.now(),
                        },
                    )
                    DemandSlot.objects.filter(schedule=schedule).delete()
                    bulk_d = [
                        DemandSlot(
                            schedule=schedule,
                            slot_index=slot["slot_index"],
                            slot_time=slot["slot_time"],
                            demand_mw=demand_mw,
                        )
                        for slot in generate_day_slots()
                    ]
                    DemandSlot.objects.bulk_create(bulk_d)

                gs, _ = GeneratorSupplySchedule.objects.update_or_create(
                    consumer=consumer,
                    date=req_date,
                    defaults={"submitted_by_user": generator},
                )
                GeneratorSupplySlot.objects.filter(schedule=gs).delete()
                bulk_g = [
                    GeneratorSupplySlot(
                        schedule=gs,
                        slot_index=slot["slot_index"],
                        slot_time=slot["slot_time"],
                        supply_mwh=supply_mwh,
                    )
                    for slot in generate_day_slots()
                ]
                GeneratorSupplySlot.objects.bulk_create(bulk_g)

            self.stdout.write(
                self.style.SUCCESS(
                    f"Seeded consumer={consumer.name!r}: {len(plants)} plant(s), {len(dates)} day(s) "
                    f"({dates[0]} … {dates[-1]}). Demand {demand_mw} MW/slot, supply {supply_mwh} MWh/slot."
                )
            )

        self.stdout.write(f"Generator supply submitted as: {generator.email}")
