import datetime

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from accounts.models import CustomUser
from core.models import Consumer, Plant, PlantTransmissionLoss, PlantUser


class Command(BaseCommand):
    help = 'Seed demo logins: Platform Admin, Consumer Manager, Plant User, Generator.'

    def add_arguments(self, parser):
        parser.add_argument('--platform-email', default='platformadmin@example.com')
        parser.add_argument('--platform-password', default='Platform@123')
        parser.add_argument('--platform-name', default='Platform Admin')

        parser.add_argument('--consumer-manager-email', default='consumermanager@example.com')
        parser.add_argument('--consumer-manager-password', default='Consumer@123')
        parser.add_argument('--consumer-manager-name', default='Consumer Manager')
        parser.add_argument('--consumer-name', default='Example Consumer')

        parser.add_argument('--plant-user-email', default='plantuser@example.com')
        parser.add_argument('--plant-user-password', default='Plant@123')
        parser.add_argument('--plant-user-name', default='Plant User')
        parser.add_argument('--plant-name', default='Dahej')
        parser.add_argument('--plant-location', default='GJ')
        parser.add_argument('--grid-tariff-per-unit', default='10.0000')
        parser.add_argument('--re-tariff-per-unit', default='8.0000')

        parser.add_argument('--generator-email', default='generator@example.com')
        parser.add_argument('--generator-password', default='Generator@123')
        parser.add_argument('--generator-name', default='Generator')

        parser.add_argument('--transmission-loss-year', type=int, default=None)
        # Backward-compatible: if only transmission-loss-value is provided,
        # we use it for both state + central.
        parser.add_argument('--transmission-loss-value', default='2.5000')
        parser.add_argument('--state-transition-loss-value', default=None)
        parser.add_argument('--central-transmission-loss-value', default=None)

        parser.add_argument('--reset-password', action='store_true')

    @transaction.atomic
    def handle(self, *args, **options):
        year = options['transmission_loss_year']
        if year is None:
            year = timezone.localdate().year

        platform_email = options['platform_email']
        platform_password = options['platform_password']
        platform_name = options['platform_name']

        consumer_manager_email = options['consumer_manager_email']
        consumer_manager_password = options['consumer_manager_password']
        consumer_manager_name = options['consumer_manager_name']
        consumer_name = options['consumer_name']

        plant_user_email = options['plant_user_email']
        plant_user_password = options['plant_user_password']
        plant_user_name = options['plant_user_name']
        plant_name = options['plant_name']
        plant_location = options['plant_location']

        tl_value = options['transmission_loss_value']

        state_tl_value = options.get('state_transition_loss_value') or tl_value
        central_tl_value = options.get('central_transmission_loss_value') or tl_value

        grid_tariff_per_unit = options['grid_tariff_per_unit']
        re_tariff_per_unit = options['re_tariff_per_unit']

        generator_email = options['generator_email']
        generator_password = options['generator_password']
        generator_name = options['generator_name']

        platform_admin, _ = CustomUser.objects.get_or_create(
            email=platform_email,
            defaults={
                'name': platform_name,
                'role': CustomUser.Role.PLATFORM_ADMIN,
                'is_active': True,
            },
        )
        if options['reset_password'] or not platform_admin.check_password(platform_password):
            platform_admin.set_password(platform_password)
            platform_admin.save(update_fields=['password'])

        consumer_manager_user, _ = CustomUser.objects.get_or_create(
            email=consumer_manager_email,
            defaults={
                'name': consumer_manager_name,
                'role': CustomUser.Role.CONSUMER_MANAGER,
                'is_active': True,
            },
        )
        if options['reset_password'] or not consumer_manager_user.check_password(consumer_manager_password):
            consumer_manager_user.set_password(consumer_manager_password)
            consumer_manager_user.save(update_fields=['password'])

        consumer, _ = Consumer.objects.get_or_create(
            name=consumer_name,
            defaults={'created_by': platform_admin, 'consumer_manager': consumer_manager_user},
        )
        if consumer.created_by_id != platform_admin.id:
            consumer.created_by = platform_admin
        consumer.consumer_manager = consumer_manager_user
        consumer.save(update_fields=['created_by', 'consumer_manager'])

        plant_user, _ = CustomUser.objects.get_or_create(
            email=plant_user_email,
            defaults={
                'name': plant_user_name,
                'role': CustomUser.Role.PLANT_USER,
                'is_active': True,
            },
        )
        if options['reset_password'] or not plant_user.check_password(plant_user_password):
            plant_user.set_password(plant_user_password)
            plant_user.save(update_fields=['password'])

        # Unique constraint is (name, consumer); lookup by those, then update location/tariffs.
        plant, _ = Plant.objects.get_or_create(
            name=plant_name,
            consumer=consumer,
            defaults={'location': plant_location},
        )
        plant.location = plant_location
        plant.grid_tariff_per_unit = grid_tariff_per_unit
        plant.re_tariff_per_unit = re_tariff_per_unit
        plant.save(update_fields=['location', 'grid_tariff_per_unit', 're_tariff_per_unit'])

        PlantUser.objects.get_or_create(user=plant_user, plant=plant)
        PlantTransmissionLoss.objects.update_or_create(
            plant=plant,
            year=year,
            defaults={
                'transmission_loss_percent': central_tl_value,
                'state_transition_loss_percent': state_tl_value,
                'central_transmission_loss_percent': central_tl_value,
            },
        )

        generator_user, _ = CustomUser.objects.get_or_create(
            email=generator_email,
            defaults={
                'name': generator_name,
                'role': CustomUser.Role.GENERATOR,
                'is_active': True,
            },
        )
        if options['reset_password'] or not generator_user.check_password(generator_password):
            generator_user.set_password(generator_password)
            generator_user.save(update_fields=['password'])

        self.stdout.write(self.style.SUCCESS('Seed complete. Demo credentials:'))
        self.stdout.write(f'Platform Admin: {platform_email} / {platform_password}')
        self.stdout.write(f'Consumer Manager: {consumer_manager_email} / {consumer_manager_password}')
        self.stdout.write(f'Plant User: {plant_user_email} / {plant_user_password}')
        self.stdout.write(f'Generator: {generator_email} / {generator_password}')

