"""
Populate the Dahej plant's slot-wise tariff difference (96 × 15-min slots).

Exact values: 2.30 and 3.15 Rs/unit — see core.tariff_presets.DAHEJ_SLOT_TARIFF.

If you still see 3.20 for some slots, the plant may still have legacy 24 hourly
tariffs in the DB. Run this command to replace with explicit 96 values.
"""

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import Plant
from core.tariff_presets import DAHEJ_SLOT_TARIFF


class Command(BaseCommand):
    help = "Fill Dahej plant's 96 slot tariffs (2.30 / 3.15). Replaces legacy 24-hour data."

    @transaction.atomic
    def handle(self, *args, **options):
        plant = Plant.objects.filter(name__iexact="Dahej").first()
        if not plant:
            self.stderr.write("Dahej plant not found.")
            return

        avg_diff = sum(DAHEJ_SLOT_TARIFF) / len(DAHEJ_SLOT_TARIFF)
        plant.hourly_tariff_difference = list(DAHEJ_SLOT_TARIFF)
        plant.grid_tariff_per_unit = Decimal(str(round(avg_diff, 4)))
        plant.re_tariff_per_unit = Decimal("0")
        plant.save(update_fields=["hourly_tariff_difference", "grid_tariff_per_unit", "re_tariff_per_unit"])

        self.stdout.write(
            self.style.SUCCESS(
                "Updated Dahej: 96 explicit slot values (2.30 / 3.15). "
                f"Avg: {avg_diff:.4f} Rs/u. Reload Plant management if the UI was open."
            )
        )
