from django.utils import timezone
from rest_framework import serializers

from accounts.models import CustomUser
from core.models import Consumer, Plant, PlantTransmissionLoss, PlantUser


class ConsumerManagerCreateSerializer(serializers.Serializer):
    consumer_name = serializers.CharField(max_length=255)
    manager_name = serializers.CharField(max_length=255)
    manager_email = serializers.EmailField()
    manager_password = serializers.CharField(write_only=True, min_length=8)

    def validate_manager_email(self, value):
        if CustomUser.objects.filter(email=value).exists():
            raise serializers.ValidationError('Email already exists')
        return value


class PlantUserCreateSerializer(serializers.Serializer):
    plant_name = serializers.CharField(max_length=255)
    location = serializers.CharField(max_length=512)
    max_consumption_per_day = serializers.DecimalField(max_digits=12, decimal_places=4)

    # Slot-wise tariff difference (Grid − RE), Rs/unit, 96 × 15-min slots.
    hourly_tariff_difference = serializers.ListField(
        child=serializers.DecimalField(max_digits=12, decimal_places=4),
        min_length=96,
        max_length=96,
        allow_empty=False,
    )

    transmission_loss_year = serializers.IntegerField()
    state_transition_loss_value = serializers.DecimalField(max_digits=8, decimal_places=4)
    central_transmission_loss_value = serializers.DecimalField(max_digits=8, decimal_places=4)

    # Employee/ERP identifier for the plant user (used later to integrate ERP).
    plant_user_empid = serializers.CharField(max_length=64)
    plant_user_email = serializers.EmailField()
    plant_user_password = serializers.CharField(write_only=True, min_length=8)

    def validate_plant_user_email(self, value):
        if CustomUser.objects.filter(email=value).exists():
            raise serializers.ValidationError('Email already exists')
        return value


class PlantUserCreateForConsumerSerializer(serializers.Serializer):
    consumer_id = serializers.IntegerField()
    plant_name = serializers.CharField(max_length=255)
    location = serializers.CharField(max_length=512)
    max_consumption_per_day = serializers.DecimalField(max_digits=12, decimal_places=4)

    # Slot-wise tariff difference (Grid − RE), Rs/unit, 96 × 15-min slots.
    hourly_tariff_difference = serializers.ListField(
        child=serializers.DecimalField(max_digits=12, decimal_places=4),
        min_length=96,
        max_length=96,
        allow_empty=False,
    )

    transmission_loss_year = serializers.IntegerField()
    state_transition_loss_value = serializers.DecimalField(max_digits=8, decimal_places=4)
    central_transmission_loss_value = serializers.DecimalField(max_digits=8, decimal_places=4)

    # Employee/ERP identifier for the plant user (used later to integrate ERP).
    plant_user_empid = serializers.CharField(max_length=64)
    plant_user_email = serializers.EmailField()
    plant_user_password = serializers.CharField(write_only=True, min_length=8)

    def validate_plant_user_email(self, value):
        if CustomUser.objects.filter(email=value).exists():
            raise serializers.ValidationError('Email already exists')
        return value


class PlantTariffUpdateSerializer(serializers.Serializer):
    max_consumption_per_day = serializers.DecimalField(max_digits=12, decimal_places=4, required=False)
    hourly_tariff_difference = serializers.ListField(
        child=serializers.DecimalField(max_digits=12, decimal_places=4),
        min_length=96,
        max_length=96,
        allow_empty=False,
    )


class PlantTransmissionLossUpdateSerializer(serializers.Serializer):
    year = serializers.IntegerField(min_value=1970)
    state_transition_loss_value = serializers.DecimalField(max_digits=8, decimal_places=4)
    central_transmission_loss_value = serializers.DecimalField(max_digits=8, decimal_places=4)


class PlantListItemSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    location = serializers.CharField()
    transmission_loss_year = serializers.IntegerField()
    state_transition_loss_value = serializers.DecimalField(max_digits=8, decimal_places=4)
    central_transmission_loss_value = serializers.DecimalField(max_digits=8, decimal_places=4)


class ConsumerListItemSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()


def current_year() -> int:
    return timezone.now().year


def get_or_none_transmission_loss(plant: Plant, year: int):
    return PlantTransmissionLoss.objects.filter(plant=plant, year=year).first()


def get_transmission_loss_percent(plant: Plant | None, year: int | None = None) -> "Decimal":
    """Returns transmission loss % (0-100) for plant. Uses total (state+central)."""
    from decimal import Decimal

    if plant is None:
        return Decimal("0")
    if year is None:
        year = timezone.now().year
    tpl = get_or_none_transmission_loss(plant, year)
    if tpl:
        return tpl.transmission_loss_percent
    return Decimal("0")


def net_to_gross(net_value: "Decimal", loss_percent: "Decimal") -> "Decimal":
    """Convert demand at plant (net) to procurement need (gross). gross = net / (1 - loss/100)."""
    from decimal import Decimal

    if loss_percent >= 100:
        return net_value  # Avoid div by zero
    factor = Decimal("1") - (loss_percent / Decimal("100"))
    if factor <= 0:
        return net_value
    return net_value / factor


def gross_to_net(gross_value: "Decimal", loss_percent: "Decimal") -> "Decimal":
    """Convert allocated (gross) to energy at plant (net). net = gross * (1 - loss/100)."""
    from decimal import Decimal

    factor = Decimal("1") - (loss_percent / Decimal("100"))
    return gross_value * factor


def net_to_gross_additive(
    net_value: "Decimal",
    state_loss_percent: "Decimal",
    central_loss_percent: "Decimal",
) -> "Decimal":
    """
    Convert net demand to gross using additive formula (matches Demand Entry).
    gross = net + net * (state + central) / 100 = net * (1 + total_loss_pct / 100).
    """
    from decimal import Decimal

    total_pct = (state_loss_percent or Decimal("0")) + (central_loss_percent or Decimal("0"))
    return net_value * (Decimal("1") + total_pct / Decimal("100"))

