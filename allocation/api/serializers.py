import datetime
import re

from django.utils import timezone
from rest_framework import serializers

APPROVED_REVISION_PATTERN = re.compile(r'^(submitted|revision[123]|upload_\d+)$')


def validate_approved_revision_value(value):
    v = (value or 'revision2').strip()
    if not APPROVED_REVISION_PATTERN.match(v):
        raise serializers.ValidationError('Invalid approved_revision.')
    return v

from allocation.slot_utils import SLOTS_PER_DAY, generate_day_slots


class DemandSlotInputSerializer(serializers.Serializer):
    slot_index = serializers.IntegerField(min_value=1, max_value=SLOTS_PER_DAY)
    demand_mw = serializers.DecimalField(max_digits=12, decimal_places=4)


class DemandEntryCreateSerializer(serializers.Serializer):
    date = serializers.DateField()
    shutdown = serializers.BooleanField(default=False)
    slots = serializers.ListField(child=DemandSlotInputSerializer(), allow_empty=False)

    def validate_date(self, value: datetime.date):
        today = timezone.localdate()
        max_date = today + datetime.timedelta(days=30)  # 31 days: today..+30
        if value < today:
            raise serializers.ValidationError('Demand entry is only allowed from today onwards.')
        if value > max_date:
            raise serializers.ValidationError('You can only schedule for the next 30 days.')
        return value

    def validate(self, attrs):
        shutdown = attrs.get('shutdown', False)
        slots = attrs.get('slots', [])

        # Always validate slot structure to keep client/server consistent.
        if len(slots) != SLOTS_PER_DAY:
            raise serializers.ValidationError(f'Exactly {SLOTS_PER_DAY} slots are required.')

        slot_indexes = sorted([s['slot_index'] for s in slots])
        expected = [d['slot_index'] for d in generate_day_slots()]
        if slot_indexes != expected:
            raise serializers.ValidationError(f'Slot indexes must be exactly 1..{SLOTS_PER_DAY} in order.')

        if shutdown:
            # Client may still send values; we will overwrite them to 0 server-side.
            pass

        return attrs


class DemandEntryResponseSlotSerializer(serializers.Serializer):
    slot_index = serializers.IntegerField()
    slot_time = serializers.CharField()
    demand_mw = serializers.DecimalField(max_digits=12, decimal_places=4)


class DemandEntryResponseSerializer(serializers.Serializer):
    date = serializers.DateField()
    shutdown = serializers.BooleanField()
    slots = serializers.ListField(child=DemandEntryResponseSlotSerializer())


class GeneratorSupplySlotInputSerializer(serializers.Serializer):
    slot_index = serializers.IntegerField(min_value=1, max_value=SLOTS_PER_DAY)
    supply_mwh = serializers.DecimalField(max_digits=12, decimal_places=4)


class GeneratorSupplySubmitSerializer(serializers.Serializer):
    consumer_manager_user_id = serializers.IntegerField()
    date = serializers.DateField()
    shutdown = serializers.BooleanField(default=False)
    slots = serializers.ListField(child=GeneratorSupplySlotInputSerializer(), allow_empty=False)

    def validate(self, attrs):
        slots = attrs.get('slots', [])
        if len(slots) != SLOTS_PER_DAY:
            raise serializers.ValidationError(f'Exactly {SLOTS_PER_DAY} slots are required.')
        expected = [d['slot_index'] for d in generate_day_slots()]
        slot_indexes = sorted([s['slot_index'] for s in slots])
        if slot_indexes != expected:
            raise serializers.ValidationError(f'Slot indexes must be exactly 1..{SLOTS_PER_DAY} in order.')
        return attrs


class IexMcpSlotInputSerializer(serializers.Serializer):
    slot_index = serializers.IntegerField(min_value=1, max_value=SLOTS_PER_DAY)
    mcp_rs_per_mwh = serializers.DecimalField(max_digits=14, decimal_places=4)


class IexMcpUpsertSerializer(serializers.Serializer):
    date = serializers.DateField()
    slots = serializers.ListField(child=IexMcpSlotInputSerializer(), allow_empty=False)

    def validate(self, attrs):
        slots = attrs.get('slots', [])
        if len(slots) != SLOTS_PER_DAY:
            raise serializers.ValidationError(f'Exactly {SLOTS_PER_DAY} slots are required.')
        expected = [d['slot_index'] for d in generate_day_slots()]
        slot_indexes = sorted([s['slot_index'] for s in slots])
        if slot_indexes != expected:
            raise serializers.ValidationError(f'Slot indexes must be exactly 1..{SLOTS_PER_DAY} in order.')
        return attrs


class IexMcpSlotResponseSerializer(serializers.Serializer):
    slot_index = serializers.IntegerField()
    slot_time = serializers.CharField()
    mcp_rs_per_mwh = serializers.DecimalField(max_digits=14, decimal_places=4)


class IexMcpResponseSerializer(serializers.Serializer):
    date = serializers.DateField()
    slots = serializers.ListField(child=IexMcpSlotResponseSerializer())


class SlotApproveSerializer(serializers.Serializer):
    date = serializers.DateField()
    slot_index = serializers.IntegerField(min_value=1, max_value=SLOTS_PER_DAY)
    consumer_id = serializers.IntegerField()
    approved_revision = serializers.CharField(max_length=32, required=False, default='revision2')

    def validate_approved_revision(self, value):
        return validate_approved_revision_value(value)

    def validate_date(self, value: datetime.date):
        from allocation.recommendation_context import validate_allocation_date

        try:
            return validate_allocation_date(value)
        except ValueError as e:
            raise serializers.ValidationError(str(e)) from e


class SlotApproveBulkItemSerializer(serializers.Serializer):
    slot_index = serializers.IntegerField(min_value=1, max_value=SLOTS_PER_DAY)
    approved_revision = serializers.CharField(max_length=32, required=False, default='revision2')

    def validate_approved_revision(self, value):
        return validate_approved_revision_value(value)


class SlotApproveBulkSerializer(serializers.Serializer):
    date = serializers.DateField()
    consumer_id = serializers.IntegerField()
    slots = serializers.ListField(
        child=SlotApproveBulkItemSerializer(),
        min_length=1,
        max_length=SLOTS_PER_DAY,
    )

    def validate_date(self, value: datetime.date):
        from allocation.recommendation_context import validate_allocation_date

        try:
            return validate_allocation_date(value)
        except ValueError as e:
            raise serializers.ValidationError(str(e)) from e

    def validate_slots(self, value):
        seen = set()
        for item in value:
            idx = int(item['slot_index'])
            if idx in seen:
                raise serializers.ValidationError(f'Duplicate slot_index: {idx}.')
            seen.add(idx)
        return value


class SlotOverridePlantItemSerializer(serializers.Serializer):
    plant_id = serializers.IntegerField()
    mwh = serializers.FloatField()


class SlotOverrideApproveSerializer(serializers.Serializer):
    date = serializers.DateField()
    slot_index = serializers.IntegerField(min_value=1, max_value=SLOTS_PER_DAY)
    consumer_id = serializers.IntegerField()
    allocations = serializers.ListField(child=SlotOverridePlantItemSerializer(), allow_empty=True)
    approved_revision = serializers.CharField(max_length=32, required=False, default='revision2')

    def validate_approved_revision(self, value):
        return validate_approved_revision_value(value)

    def validate_date(self, value: datetime.date):
        from allocation.recommendation_context import validate_allocation_date

        try:
            return validate_allocation_date(value)
        except ValueError as e:
            raise serializers.ValidationError(str(e)) from e


class SlotRevokeSerializer(serializers.Serializer):
    date = serializers.DateField()
    slot_index = serializers.IntegerField(min_value=1, max_value=SLOTS_PER_DAY)
    consumer_id = serializers.IntegerField()

    def validate_date(self, value: datetime.date):
        from allocation.recommendation_context import validate_allocation_date

        try:
            return validate_allocation_date(value)
        except ValueError as e:
            raise serializers.ValidationError(str(e)) from e

