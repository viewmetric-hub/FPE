"""
Add Selaqui plant with slot-wise tariff difference (96 × 15-min) and transmission loss.
Usage: python manage.py add_selaqui_plant

Tariff values: core.tariff_presets.SELAQUI_SLOT_TARIFF (96 slots).
To refresh tariffs on an existing plant: python manage.py seed_selaqui_tariff
"""
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from accounts.models import CustomUser
from core.models import Consumer, Plant, PlantTransmissionLoss, PlantUser
from core.tariff_presets import SELAQUI_SLOT_TARIFF


class Command(BaseCommand):
    help = "Add Selaqui plant with slot tariffs and transmission loss details"

    def add_arguments(self, parser):
        parser.add_argument(
            "--consumer-name",
            default="Example Consumer",
            help="Consumer under which to add the plant",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        consumer_name = options["consumer_name"]
        consumer = Consumer.objects.filter(name=consumer_name).first()
        if not consumer:
            self.stderr.write(self.style.ERROR(f"Consumer '{consumer_name}' not found."))
            return

        plant_name = "Selaqui"
        if Plant.objects.filter(name=plant_name, consumer=consumer).exists():
            self.stdout.write(self.style.WARNING(f"Plant '{plant_name}' already exists under {consumer_name}."))
            return

        avg_diff = sum(SELAQUI_SLOT_TARIFF) / len(SELAQUI_SLOT_TARIFF)

        plant = Plant.objects.create(
            name=plant_name,
            location="UK",
            consumer=consumer,
            hourly_tariff_difference=SELAQUI_SLOT_TARIFF,
            grid_tariff_per_unit=Decimal(str(round(avg_diff, 4))),
            re_tariff_per_unit=Decimal("0"),
        )

        plant_user_email = "selaqui@example.com"
        if CustomUser.objects.filter(email=plant_user_email).exists():
            self.stderr.write(self.style.ERROR(f"User {plant_user_email} already exists. Use a different email."))
            return

        plant_user = CustomUser.objects.create_user(
            email=plant_user_email,
            password="admin@123",
            name="008",
            role=CustomUser.Role.PLANT_USER,
            is_active=True,
        )

        PlantUser.objects.create(user=plant_user, plant=plant)

        state_loss = Decimal("8")
        central_loss = Decimal("4")
        total_loss = state_loss + central_loss

        PlantTransmissionLoss.objects.create(
            plant=plant,
            year=2026,
            transmission_loss_percent=total_loss,
            state_transition_loss_percent=state_loss,
            central_transmission_loss_percent=central_loss,
        )

        self.stdout.write(self.style.SUCCESS(f"Plant '{plant_name}' created successfully."))
        self.stdout.write(f"  Plant ID: {plant.id}")
        self.stdout.write(f"  Location: UK")
        self.stdout.write(f"  Plant User Email: {plant_user_email}")
        self.stdout.write(f"  Plant User Password: admin@123")
        self.stdout.write(f"  Transmission Loss: State {state_loss}%, Central {central_loss}%")
