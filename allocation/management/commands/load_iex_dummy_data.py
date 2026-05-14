"""
Load 7–10 days of dummy IEX historical data into IexGreenDayAheadMcpSlot.
Realistic pattern: morning moderate, afternoon lower (solar), evening higher.
"""

import datetime
import random
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from allocation.models import IexGreenDayAheadMcpSlot
from allocation.slot_utils import SLOTS_PER_DAY, generate_day_slots


def _mcp_for_slot(slot_index: int, base_offset: float = 0) -> float:
    """
    Realistic MCP (Rs/MWh) by time of day.
    Slot 1–24: night (01:00–06:00) → ~4000
    Slot 25–40: morning (07:00–10:00) → ~4500
    Slot 41–68: afternoon (11:00–17:00) → ~2500 (solar effect)
    Slot 69–96: evening (18:00–24:00) → ~5500
    """
    hour = (slot_index - 1) // 4 + 1
    if 1 <= hour <= 6:
        base = 3800
    elif 7 <= hour <= 10:
        base = 4400
    elif 11 <= hour <= 17:
        base = 2300
    else:
        base = 5300
    # Small random variation (±5%)
    var = random.uniform(-0.05, 0.05) * base
    return max(500, base + var + base_offset)


class Command(BaseCommand):
    help = "Generate 7–10 days of dummy IEX historical data for prediction"

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=7,
            help="Number of days to generate (default: 7)",
        )
    def handle(self, *args, **options):
        days = min(max(options["days"], 7), 14)
        today = datetime.date.today()
        slot_times = {s["slot_index"]: s["slot_time"] for s in generate_day_slots()}

        created = 0
        with transaction.atomic():
            for i in range(days, 0, -1):
                d = today - datetime.timedelta(days=i)
                IexGreenDayAheadMcpSlot.objects.filter(date=d).delete()

                bulk = []
                for slot_idx in range(1, SLOTS_PER_DAY + 1):
                    mcp = _mcp_for_slot(slot_idx)
                    bulk.append(
                        IexGreenDayAheadMcpSlot(
                            date=d,
                            slot_index=slot_idx,
                            slot_time=slot_times[slot_idx],
                            mcp_rs_per_mwh=Decimal(str(round(mcp, 2))),
                        )
                    )
                IexGreenDayAheadMcpSlot.objects.bulk_create(bulk)
                created += len(bulk)

        self.stdout.write(
            self.style.SUCCESS(f"Created {created} slot records ({days} days)")
        )
