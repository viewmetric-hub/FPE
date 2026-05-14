from django.urls import path

from .views import (
    CreateConsumerManagerView,
    CreatePlantUserView,
    PlatformConsumerListView,
    CreatePlantUserForConsumerView,
    ParseTariffExcelView,
    PlantListView,
    PlantTariffUpdateView,
    PlantTransmissionLossUpdateView,
)

app_name = 'core_api'

urlpatterns = [
    path('create-consumer-manager/', CreateConsumerManagerView.as_view(), name='create_consumer_manager'),
    path('create-plant-user/', CreatePlantUserView.as_view(), name='create_plant_user'),
    path('consumers/', PlatformConsumerListView.as_view(), name='consumer_list'),
    path('create-plant-user-for-consumer/', CreatePlantUserForConsumerView.as_view(), name='create_plant_user_for_consumer'),
    path('plants/', PlantListView.as_view(), name='plant_list'),
    path('plants/parse-tariff-excel/', ParseTariffExcelView.as_view(), name='parse_tariff_excel'),
    path('plants/<int:plant_id>/tariff/', PlantTariffUpdateView.as_view(), name='plant_tariff_update'),
    path('plants/<int:plant_id>/transmission-loss/', PlantTransmissionLossUpdateView.as_view(), name='plant_transmission_loss_update'),
    # Plant edit (name/location) can be added later; for now, transmission loss is the requested edit.
]

