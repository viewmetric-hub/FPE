"""
Set Selaqui plant demand: all 96 slots = 15 MW for a given date.
Usage: python manage.py set_selaqui_demand 2026-03-22
"""
import datetime

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from accounts.models import CustomUser
from allocation.models import DemandSchedule, DemandSlot
from allocation.slot_utils import generate_day_slots
from core.models import Plant, PlantUser


class Command(BaseCommand):
    help = "Set Selaqui plant demand: all 96 slots = 15 MW"

    def add_arguments(self, parser):
        parser.add_argument(
            "date",
            type=str,
            help="Date in YYYY-MM-DD format (e.g. 2026-03-22)",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        date_str = options["date"]
        try:
            req_date = datetime.date.fromisoformat(date_str)
        except ValueError:
            self.stderr.write(self.style.ERROR("Invalid date. Use YYYY-MM-DD."))
            return

        plant_user = CustomUser.objects.filter(
            email="selaqui@example.com",
            role=CustomUser.Role.PLANT_USER,
        ).first()
        if not plant_user:
            self.stderr.write(self.style.ERROR("Selaqui plant user (selaqui@example.com) not found."))
            return

        plant = PlantUser.objects.filter(user=plant_user).first()
        if not plant:
            self.stderr.write(self.style.ERROR("Selaqui plant not found."))
            return
        plant = plant.plant

        schedule, created = DemandSchedule.objects.update_or_create(
            plant=plant,
            date=req_date,
            defaults={
                "shutdown": False,
                "created_by_user": plant_user,
                "updated_at": timezone.now(),
            },
        )

        DemandSlot.objects.filter(schedule=schedule).delete()

        bulk = []
        for slot in generate_day_slots():
            bulk.append(
                DemandSlot(
                    schedule=schedule,
                    slot_index=slot["slot_index"],
                    slot_time=slot["slot_time"],
                    demand_mw=15,
                )
            )
        DemandSlot.objects.bulk_create(bulk)

        self.stdout.write(
            self.style.SUCCESS(
                f"Demand updated: Selaqui plant, {req_date}, all 96 slots = 15 MW"
            )
        )
