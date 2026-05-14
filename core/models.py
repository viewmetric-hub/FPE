from django.conf import settings
from django.db import models
from django.utils import timezone


class Consumer(models.Model):
    name = models.CharField(max_length=255, unique=True)
    # Who created this organization (Platform Admin).
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='consumers_created',
    )
    # Which Consumer Manager user owns this consumer for isolation.
    consumer_manager = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='managed_consumer',
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    def __str__(self) -> str:
        return self.name


class Plant(models.Model):
    name = models.CharField(max_length=255)
    location = models.CharField(max_length=512)
    max_consumption_per_day = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    consumer = models.ForeignKey(Consumer, on_delete=models.CASCADE, related_name='plants')
    # Tariffs used by AI allocation heuristic (profit/savings estimation).
    grid_tariff_per_unit = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    re_tariff_per_unit = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    # Slot-wise tariff difference (Grid − RE), Rs/unit, 96 values (15-min slots); two decimal places. Legacy rows may have 24 hourly values.
    hourly_tariff_difference = models.JSONField(default=list, blank=True)

    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        unique_together = [('name', 'consumer')]

    def __str__(self) -> str:
        return f'{self.name} ({self.consumer.name})'


class PlantTransmissionLoss(models.Model):
    plant = models.ForeignKey(Plant, on_delete=models.CASCADE, related_name='transmission_losses')
    year = models.PositiveIntegerField()
    transmission_loss_percent = models.DecimalField(max_digits=8, decimal_places=4)
    # Split loss components (requested by consumer manager UI).
    state_transition_loss_percent = models.DecimalField(max_digits=8, decimal_places=4, default=0)
    central_transmission_loss_percent = models.DecimalField(max_digits=8, decimal_places=4, default=0)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = [('plant', 'year')]
        indexes = [models.Index(fields=['year'])]

    def __str__(self) -> str:
        return f'{self.plant.name} - {self.year}: {self.transmission_loss_percent}% (state={self.state_transition_loss_percent}%, central={self.central_transmission_loss_percent}%)'


class PlantUser(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='plant_user_profile')
    plant = models.ForeignKey(Plant, on_delete=models.CASCADE, related_name='plant_users')

    def __str__(self) -> str:
        return f'{self.user.email} -> {self.plant.name}'

