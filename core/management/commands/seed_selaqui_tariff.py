"""
Populate the Selaqui plant's slot-wise tariff difference (96 × 15-min slots).

Exact values — see core.tariff_presets.SELAQUI_SLOT_TARIFF:
24×1.64, 12×5.41, 36×3.35, 12×5.41, 12×1.64 (Rs/unit, two decimals).
"""

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import Plant
from core.tariff_presets import SELAQUI_SLOT_TARIFF


class Command(BaseCommand):
    help = "Fill Selaqui plant's 96 slot tariffs (1.64 / 5.41 / 3.35 pattern)."

    @transaction.atomic
    def handle(self, *args, **options):
        plant = Plant.objects.filter(name__iexact="Selaqui").first()
        if not plant:
            self.stderr.write("Selaqui plant not found.")
            return

        avg_diff = sum(SELAQUI_SLOT_TARIFF) / len(SELAQUI_SLOT_TARIFF)
        plant.hourly_tariff_difference = list(SELAQUI_SLOT_TARIFF)
        plant.grid_tariff_per_unit = Decimal(str(round(avg_diff, 4)))
        plant.re_tariff_per_unit = Decimal("0")
        plant.save(update_fields=["hourly_tariff_difference", "grid_tariff_per_unit", "re_tariff_per_unit"])

        self.stdout.write(
            self.style.SUCCESS(
                "Updated Selaqui: 96 slot tariffs. "
                f"Avg: {avg_diff:.4f} Rs/u. Reload Plant management if the UI was open."
            )
        )
