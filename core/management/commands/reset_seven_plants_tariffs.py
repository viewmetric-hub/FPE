"""
Delete all plants and create seven plants per consumer with slot-wise tariff difference (Rs/unit, 96 slots).

Plant names match the tariff table column headers: GJ, AP, PJ, UK, RL, KPO, TS (each column’s 96 values).

Usage:
  python manage.py reset_seven_plants_tariffs
  python manage.py reset_seven_plants_tariffs --consumer-name "Example Consumer"
"""

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import Consumer, Plant

# Display name (DB `Plant.name`) and key into `build_tariff_columns()` dict.
PLANT_SPECS = [
    ("GJ", "gj"),
    ("AP", "ap"),
    ("PJ", "pj"),
    ("UK", "uk"),
    ("RL", "rl"),
    ("KPO", "kpo"),
    ("TS", "ts"),
]


def _append_slots(
    gj, ap, pj, uk, rl, kpo, ts, n, g, a, p, u, r, k, t
):
    for _ in range(n):
        gj.append(g)
        ap.append(a)
        pj.append(p)
        uk.append(u)
        rl.append(r)
        kpo.append(k)
        ts.append(t)


def build_tariff_columns():
    """96 × 7 tariff difference values (Rs/unit) per slot, slot index 0 = 00:00–00:15."""
    gj, ap, pj, uk, rl, kpo, ts = [], [], [], [], [], [], []
    # 1–24
    _append_slots(gj, ap, pj, uk, rl, kpo, ts, 24, 2.308, 1.48, 0.19, 1.64, -0.39, 1.15, 2.84)
    # 25–28
    _append_slots(gj, ap, pj, uk, rl, kpo, ts, 4, 2.308, 3.73, 1.19, 5.41, -0.39, 1.15, 2.84)
    # 29–32
    _append_slots(gj, ap, pj, uk, rl, kpo, ts, 4, 3.158, 3.73, 1.19, 5.41, -0.39, 1.15, 2.84)
    # 33–36
    _append_slots(gj, ap, pj, uk, rl, kpo, ts, 4, 3.158, 3.73, 1.19, 5.41, -0.59, 0.95, 2.84)
    # 37–48
    _append_slots(gj, ap, pj, uk, rl, kpo, ts, 12, 3.158, 3.73, 1.19, 3.35, -0.59, 0.95, 2.84)
    # 49–60
    _append_slots(gj, ap, pj, uk, rl, kpo, ts, 12, 2.308, 1.48, 1.19, 3.35, -0.59, 0.95, 2.84)
    # 61–64
    _append_slots(gj, ap, pj, uk, rl, kpo, ts, 4, 2.308, 2.23, 1.19, 3.35, -0.59, 0.95, 2.84)
    # 65–72
    _append_slots(gj, ap, pj, uk, rl, kpo, ts, 8, 2.308, 2.23, 1.19, 3.35, -0.39, 1.15, 2.84)
    # 73–88
    _append_slots(gj, ap, pj, uk, rl, kpo, ts, 16, 3.158, 3.73, 1.19, 5.41, -0.09, 1.45, 2.84)
    # 89–96
    _append_slots(gj, ap, pj, uk, rl, kpo, ts, 8, 2.308, 2.23, 0.19, 1.64, -0.09, 1.45, 2.84)

    cols = {
        "gj": gj,
        "ap": ap,
        "pj": pj,
        "uk": uk,
        "rl": rl,
        "kpo": kpo,
        "ts": ts,
    }
    for k, v in cols.items():
        if len(v) != 96:
            raise ValueError(f"Column {k} expected 96 slots, got {len(v)}")
    return cols


class Command(BaseCommand):
    help = "Delete plants and create GJ, AP, PJ, UK, RL, KPO, TS with the shared 96-slot tariff table."

    def add_arguments(self, parser):
        parser.add_argument(
            "--consumer-name",
            type=str,
            default="",
            help="Only recreate plants for this consumer name (default: all consumers).",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        name_filter = (options.get("consumer_name") or "").strip()
        if name_filter:
            consumers = list(Consumer.objects.filter(name__iexact=name_filter).order_by("id"))
            if not consumers:
                self.stderr.write(self.style.ERROR(f'No consumer named "{name_filter}".'))
                return
            deleted, _ = Plant.objects.filter(consumer__in=consumers).delete()
            self.stdout.write(
                self.style.WARNING(
                    f'Deleted plants for consumer "{name_filter}" (related rows count): {deleted}'
                )
            )
        else:
            consumers = list(Consumer.objects.all().order_by("id"))
            if not consumers:
                self.stderr.write(self.style.ERROR("No consumers in database. Create a consumer first."))
                return
            deleted, _ = Plant.objects.all().delete()
            self.stdout.write(self.style.WARNING(f"Deleted all existing plant-related rows (including plants): {deleted}"))

        cols = build_tariff_columns()

        created_total = 0
        for consumer in consumers:
            for plant_name, tag in PLANT_SPECS:
                series = cols[tag]
                avg_diff = sum(series) / len(series)
                Plant.objects.create(
                    name=plant_name,
                    location="—",
                    consumer=consumer,
                    grid_tariff_per_unit=Decimal(str(round(avg_diff, 4))),
                    re_tariff_per_unit=Decimal("0"),
                    hourly_tariff_difference=[round(float(x), 4) for x in series],
                )
                created_total += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Created {created_total} plants across {len(consumers)} consumer(s): "
                f"{', '.join(p[0] for p in PLANT_SPECS)}. "
                f"Each plant is named after its column (GJ … TS) with that column’s 96 slot values."
            )
        )
