"""
Create demand (96 slots) for all plants and generator supply (96 slots) for each of the next 7 days.

Usage:
  python manage.py seed_next_7_days_demand_and_supply
  python manage.py seed_next_7_days_demand_and_supply --consumer-name "Example Consumer"
  python manage.py seed_next_7_days_demand_and_supply --dahej-mw 12 --selaqui-mw 15 --supply-mwh 16.12
"""

import datetime
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from accounts.models import CustomUser
from allocation.models import DemandSchedule, DemandSlot, GeneratorSupplySchedule, GeneratorSupplySlot
from allocation.slot_utils import generate_day_slots
from core.models import Consumer, Plant, PlantUser


def _default_supply_mwh_for_date(req_date: datetime.date) -> Decimal:
    """Match generator_allocation_edit.html defaults: tomorrow 16.12, Mondays 15.21, else 16.12."""
    today = timezone.localdate()
    tomorrow = today + datetime.timedelta(days=1)
    if req_date == tomorrow:
        return Decimal("16.12")
    if req_date.weekday() == 0:  # Monday
        return Decimal("15.21")
    return Decimal("16.12")


class Command(BaseCommand):
    help = "Seed demand for all plants and generator supply for tomorrow through tomorrow+6 (7 days)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--consumer-name",
            default="Example Consumer",
            help="Consumer whose plants and generator supply to fill",
        )
        parser.add_argument(
            "--dahej-mw",
            type=float,
            default=12.0,
            help="Demand MW per slot for plant named Dahej (default 12)",
        )
        parser.add_argument(
            "--selaqui-mw",
            type=float,
            default=15.0,
            help="Demand MW per slot for plant named Selaqui (default 15)",
        )
        parser.add_argument(
            "--default-mw",
            type=float,
            default=12.0,
            help="Demand MW per slot for any other plant (default 12)",
        )
        parser.add_argument(
            "--supply-mwh",
            type=float,
            default=None,
            help="Override: same supply MWh for every slot every day (default: use date-based 16.12/15.21)",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        consumer_name = options["consumer_name"]
        consumer = Consumer.objects.filter(name=consumer_name).first()
        if not consumer:
            self.stderr.write(self.style.ERROR(f"Consumer '{consumer_name}' not found."))
            return

        generator = CustomUser.objects.filter(role=CustomUser.Role.GENERATOR, is_active=True).first()
        if not generator:
            self.stderr.write(
                self.style.ERROR("No active Generator user found. Run seed_platform_demo or create a Generator user.")
            )
            return

        today = timezone.localdate()
        dates = [today + datetime.timedelta(days=i) for i in range(1, 8)]  # tomorrow .. +7 days

        plants = list(Plant.objects.filter(consumer=consumer).order_by("id"))
        if not plants:
            self.stderr.write(self.style.ERROR(f"No plants under consumer '{consumer_name}'."))
            return

        dahej_mw = Decimal(str(options["dahej_mw"]))
        selaqui_mw = Decimal(str(options["selaqui_mw"]))
        default_mw = Decimal(str(options["default_mw"]))

        def demand_mw_for_plant(plant: Plant) -> Decimal:
            name = (plant.name or "").strip().lower()
            if name == "dahej":
                return dahej_mw
            if name == "selaqui":
                return selaqui_mw
            return default_mw

        for req_date in dates:
            # --- Demand per plant ---
            for plant in plants:
                plant_user = PlantUser.objects.filter(plant=plant).select_related("user").first()
                if not plant_user:
                    self.stderr.write(self.style.WARNING(f"No PlantUser for {plant.name}; skipping demand for that plant."))
                    continue
                creator = plant_user.user
                mw = demand_mw_for_plant(plant)

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
                bulk_d = []
                for slot in generate_day_slots():
                    bulk_d.append(
                        DemandSlot(
                            schedule=schedule,
                            slot_index=slot["slot_index"],
                            slot_time=slot["slot_time"],
                            demand_mw=mw,
                        )
                    )
                DemandSlot.objects.bulk_create(bulk_d)

            # --- Generator supply (one schedule per consumer per day) ---
            if options["supply_mwh"] is not None:
                supply_val = Decimal(str(options["supply_mwh"]))
            else:
                supply_val = _default_supply_mwh_for_date(req_date)

            schedule_gs, _ = GeneratorSupplySchedule.objects.update_or_create(
                consumer=consumer,
                date=req_date,
                defaults={"submitted_by_user": generator},
            )
            GeneratorSupplySlot.objects.filter(schedule=schedule_gs).delete()
            bulk_g = []
            for slot in generate_day_slots():
                bulk_g.append(
                    GeneratorSupplySlot(
                        schedule=schedule_gs,
                        slot_index=slot["slot_index"],
                        slot_time=slot["slot_time"],
                        supply_mwh=supply_val,
                    )
                )
            GeneratorSupplySlot.objects.bulk_create(bulk_g)

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. Consumer={consumer_name!r}, dates {dates[0]} .. {dates[-1]} "
                f"({len(dates)} days). Plants: {', '.join(p.name for p in plants)}. "
                f"Demand: Dahej={dahej_mw} MW/slot, Selaqui={selaqui_mw} MW/slot, other={default_mw} MW/slot."
            )
        )
        self.stdout.write(f"Generator supply submitted as user: {generator.email}")
