from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import CustomUser
from core.api.serializers import (
    ConsumerManagerCreateSerializer,
    ConsumerListItemSerializer,
    PlantTariffUpdateSerializer,
    PlantUserCreateForConsumerSerializer,
    PlantListItemSerializer,
    PlantTransmissionLossUpdateSerializer,
    PlantUserCreateSerializer,
    current_year,
    get_or_none_transmission_loss,
)
from decimal import Decimal

from core.api.utils import get_managed_consumer
from core.models import Consumer, Plant, PlantTransmissionLoss, PlantUser
from core.tariff_utils import normalize_plant_tariff_difference
from core.permissions import IsConsumerManager, IsConsumerManagerOrPlatformAdmin, IsPlatformAdmin
from core.tariff_excel import parse_tariff_excel


class CreateConsumerManagerView(APIView):
    permission_classes = [IsPlatformAdmin]

    @transaction.atomic
    def post(self, request):
        serializer = ConsumerManagerCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        user = CustomUser.objects.create_user(
            email=data['manager_email'],
            password=data['manager_password'],
            name=data['manager_name'],
            role=CustomUser.Role.CONSUMER_MANAGER,
            is_active=True,
        )

        consumer = Consumer.objects.create(
            name=data['consumer_name'],
            created_by=request.user,
            consumer_manager=user,
        )

        return Response(
            {
                'consumer_id': consumer.id,
                'consumer_name': consumer.name,
                'consumer_manager_user_id': user.id,
                'consumer_manager_email': user.email,
            },
            status=status.HTTP_201_CREATED,
        )


class CreatePlantUserView(APIView):
    permission_classes = [IsConsumerManager]

    @transaction.atomic
    def post(self, request):
        serializer = PlantUserCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        consumer = get_managed_consumer(request.user)
        if consumer is None:
            return Response({'detail': 'Consumer not linked for this manager user.'}, status=status.HTTP_400_BAD_REQUEST)

        diffs = [Decimal(str(v)) for v in data['hourly_tariff_difference']]
        avg_diff = sum(diffs) / len(diffs) if diffs else Decimal('0')
        plant = Plant.objects.create(
            name=data['plant_name'],
            location=data['location'],
            max_consumption_per_day=data['max_consumption_per_day'],
            consumer=consumer,
            hourly_tariff_difference=[float(v) for v in diffs],
            grid_tariff_per_unit=avg_diff,
            re_tariff_per_unit=Decimal('0'),
        )

        plant_user = CustomUser.objects.create_user(
            email=data['plant_user_email'],
            password=data['plant_user_password'],
            name=data['plant_user_empid'],
            role=CustomUser.Role.PLANT_USER,
            is_active=True,
        )

        PlantUser.objects.create(user=plant_user, plant=plant)

        PlantTransmissionLoss.objects.create(
            plant=plant,
            year=data['transmission_loss_year'],
            transmission_loss_percent=data['central_transmission_loss_value'],
            state_transition_loss_percent=data['state_transition_loss_value'],
            central_transmission_loss_percent=data['central_transmission_loss_value'],
        )

        return Response(
            {
                'plant_id': plant.id,
                'plant_name': plant.name,
                'max_consumption_per_day': str(plant.max_consumption_per_day),
                'transmission_loss_year': data['transmission_loss_year'],
                'state_transition_loss_value': str(data['state_transition_loss_value']),
                'central_transmission_loss_value': str(data['central_transmission_loss_value']),
                'plant_user_id': plant_user.id,
                'plant_user_email': plant_user.email,
            },
            status=status.HTTP_201_CREATED,
        )


class PlatformConsumerListView(APIView):
    permission_classes = [IsPlatformAdmin]

    def get(self, request):
        qs = Consumer.objects.all().order_by('id')
        results = [{'id': c.id, 'name': c.name} for c in qs]
        return Response({'results': results}, status=status.HTTP_200_OK)


class CreatePlantUserForConsumerView(APIView):
    permission_classes = [IsPlatformAdmin]

    @transaction.atomic
    def post(self, request):
        serializer = PlantUserCreateForConsumerSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        consumer = get_object_or_404(Consumer, id=data['consumer_id'])

        diffs = [Decimal(str(v)) for v in data['hourly_tariff_difference']]
        avg_diff = sum(diffs) / len(diffs) if diffs else Decimal('0')
        plant = Plant.objects.create(
            name=data['plant_name'],
            location=data['location'],
            max_consumption_per_day=data['max_consumption_per_day'],
            consumer=consumer,
            hourly_tariff_difference=[float(v) for v in diffs],
            grid_tariff_per_unit=avg_diff,
            re_tariff_per_unit=Decimal('0'),
        )

        plant_user = CustomUser.objects.create_user(
            email=data['plant_user_email'],
            password=data['plant_user_password'],
            name=data['plant_user_empid'],
            role=CustomUser.Role.PLANT_USER,
            is_active=True,
        )

        PlantUser.objects.create(user=plant_user, plant=plant)

        PlantTransmissionLoss.objects.create(
            plant=plant,
            year=data['transmission_loss_year'],
            transmission_loss_percent=data['central_transmission_loss_value'],
            state_transition_loss_percent=data['state_transition_loss_value'],
            central_transmission_loss_percent=data['central_transmission_loss_value'],
        )

        return Response(
            {
                'plant_id': plant.id,
                'plant_name': plant.name,
                'consumer_id': consumer.id,
                'max_consumption_per_day': str(plant.max_consumption_per_day),
                'transmission_loss_year': data['transmission_loss_year'],
                'state_transition_loss_value': str(data['state_transition_loss_value']),
                'central_transmission_loss_value': str(data['central_transmission_loss_value']),
                'plant_user_id': plant_user.id,
                'plant_user_email': plant_user.email,
            },
            status=status.HTTP_201_CREATED,
        )


class PlantListView(APIView):
    permission_classes = [IsConsumerManager]

    def get(self, request):
        consumer = get_managed_consumer(request.user)
        if consumer is None:
            return Response({'detail': 'Consumer not linked for this manager user.'}, status=status.HTTP_400_BAD_REQUEST)

        year = current_year()
        plants = (
            Plant.objects.filter(consumer=consumer)
            .select_related('consumer')
            .order_by('id')
        )

        results = []
        for p in plants:
            tpl = get_or_none_transmission_loss(p, year)
            if tpl is None:
                # Provide a default placeholder if transmission loss for current year isn't created yet.
                state_loss_value = 0
                central_loss_value = 0
                loss_year = year
            else:
                state_loss_value = tpl.state_transition_loss_percent
                central_loss_value = tpl.central_transmission_loss_percent
                loss_year = tpl.year

            raw_htd = p.hourly_tariff_difference or []
            htd = normalize_plant_tariff_difference(raw_htd)
            results.append(
                {
                    'id': p.id,
                    'name': p.name,
                    'location': p.location,
                    'max_consumption_per_day': str(p.max_consumption_per_day),
                    'hourly_tariff_difference': htd,
                    # True when DB still has legacy 24 hourly values (expanded to 96 for display).
                    'tariff_is_legacy_24h': len(raw_htd) == 24,
                    'transmission_loss_year': loss_year,
                    'state_transition_loss_value': state_loss_value,
                    'central_transmission_loss_value': central_loss_value,
                }
            )

        return Response({'results': results}, status=status.HTTP_200_OK)


class ParseTariffExcelView(APIView):
    """Parse Linde MOD/CUF Excel (TOD Hour sheet) and return plant tariffs."""
    permission_classes = [IsConsumerManagerOrPlatformAdmin]

    def post(self, request):
        if 'file' not in request.FILES:
            return Response({'detail': 'No file uploaded. Use form field "file".'}, status=status.HTTP_400_BAD_REQUEST)
        file = request.FILES['file']
        if not file.name.endswith(('.xlsx', '.xls')):
            return Response({'detail': 'File must be .xlsx or .xls'}, status=status.HTTP_400_BAD_REQUEST)
        result = parse_tariff_excel(file)
        if result['error']:
            return Response({'detail': result['error']}, status=status.HTTP_400_BAD_REQUEST)
        return Response({'plants': result['plants']}, status=status.HTTP_200_OK)


class PlantTariffUpdateView(APIView):
    """Update slot-wise tariff difference (96 values) for a plant."""
    permission_classes = [IsConsumerManagerOrPlatformAdmin]

    def patch(self, request, plant_id: int):
        serializer = PlantTariffUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if request.user.role == CustomUser.Role.PLATFORM_ADMIN:
            plant = get_object_or_404(Plant, id=plant_id)
        else:
            consumer = get_managed_consumer(request.user)
            if consumer is None:
                return Response(
                    {'detail': 'Consumer not linked for this manager user.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            plant = get_object_or_404(Plant, id=plant_id, consumer=consumer)

        diffs = [Decimal(str(v)) for v in data['hourly_tariff_difference']]
        avg_diff = sum(diffs) / len(diffs) if diffs else Decimal('0')
        plant.hourly_tariff_difference = [float(v) for v in diffs]
        plant.grid_tariff_per_unit = avg_diff
        plant.re_tariff_per_unit = Decimal('0')
        update_fields = ['hourly_tariff_difference', 'grid_tariff_per_unit', 're_tariff_per_unit']
        if 'max_consumption_per_day' in data:
            plant.max_consumption_per_day = data['max_consumption_per_day']
            update_fields.append('max_consumption_per_day')
        plant.save(update_fields=update_fields)

        return Response(
            {
                'plant_id': plant.id,
                'max_consumption_per_day': str(plant.max_consumption_per_day),
                'hourly_tariff_difference': plant.hourly_tariff_difference,
            },
            status=status.HTTP_200_OK,
        )


class PlantTransmissionLossUpdateView(APIView):
    permission_classes = [IsConsumerManagerOrPlatformAdmin]

    def patch(self, request, plant_id: int):
        serializer = PlantTransmissionLossUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if request.user.role == CustomUser.Role.PLATFORM_ADMIN:
            plant = get_object_or_404(Plant, id=plant_id)
        else:
            consumer = get_managed_consumer(request.user)
            if consumer is None:
                return Response(
                    {'detail': 'Consumer not linked for this manager user.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            plant = get_object_or_404(Plant, id=plant_id, consumer=consumer)

        tpl, _created = PlantTransmissionLoss.objects.update_or_create(
            plant=plant,
            year=data['year'],
            defaults={
                'transmission_loss_percent': data['central_transmission_loss_value'],
                'state_transition_loss_percent': data['state_transition_loss_value'],
                'central_transmission_loss_percent': data['central_transmission_loss_value'],
                'updated_at': timezone.now(),
            },
        )

        return Response(
            {
                'plant_id': plant.id,
                'year': tpl.year,
                'state_transition_loss_value': str(tpl.state_transition_loss_percent),
                'central_transmission_loss_value': str(tpl.central_transmission_loss_percent),
            },
            status=status.HTTP_200_OK,
        )

