from django.conf import settings
from django.db import models
from django.utils import timezone

from core.models import Plant, Consumer


class DemandSchedule(models.Model):
    plant = models.ForeignKey(Plant, on_delete=models.CASCADE, related_name='demand_schedules')
    date = models.DateField()
    shutdown = models.BooleanField(default=False)
    created_by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='demand_schedules_created',
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['plant', 'date'], name='unique_plant_date_schedule'),
        ]
        indexes = [models.Index(fields=['plant', 'date'])]

    def __str__(self) -> str:
        return f'{self.plant.name} - {self.date} ({self.shutdown=})'


class DemandSlot(models.Model):
    schedule = models.ForeignKey(DemandSchedule, on_delete=models.CASCADE, related_name='slots')
    slot_index = models.PositiveIntegerField()
    # Stores the time label (15 min steps) to keep server-side ordering stable.
    slot_time = models.TimeField()
    demand_mw = models.DecimalField(max_digits=12, decimal_places=4, default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['schedule', 'slot_index'], name='unique_schedule_slot_index'),
        ]
        ordering = ['slot_index']

    def __str__(self) -> str:
        return f'Slot {self.slot_index} ({self.slot_time}) - {self.demand_mw} MW'


class ConsumerDemandApproval(models.Model):
    """
    Consumer-wide approval marker for a specific day.
    We keep this separate from DemandSchedule (which is per-plant) to support
    the "approve all plants together for that consumer+date" UX.
    """

    consumer = models.ForeignKey(Consumer, on_delete=models.CASCADE, related_name='demand_approvals')
    date = models.DateField()
    approved_by_user = models.ForeignKey('accounts.CustomUser', on_delete=models.PROTECT, related_name='demand_approvals_created')
    approved_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['consumer', 'date'], name='unique_consumer_date_approval'),
        ]

    def __str__(self) -> str:
        return f'Approval: {self.consumer_id} on {self.date}'


class GeneratorScheduleApproval(models.Model):
    """
    Consumer manager approves demand for Generator visibility.
    Only when this exists for (consumer, date) is demand shown in the Generator dashboard.
    """

    consumer = models.ForeignKey(Consumer, on_delete=models.CASCADE, related_name='generator_schedule_approvals')
    date = models.DateField()
    approved_by_user = models.ForeignKey(
        'accounts.CustomUser', on_delete=models.PROTECT, related_name='generator_schedule_approvals_created'
    )
    approved_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['consumer', 'date'], name='unique_consumer_date_generator_schedule_approval'
            ),
        ]

    def __str__(self) -> str:
        return f'GeneratorScheduleApproval: {self.consumer_id} on {self.date}'


class GeneratorSupplySchedule(models.Model):
    """
    Generator submits supply/energy for a consumer manager's consumer on a given day.
    For MVP, we treat this submitted supply as "allocated" when the consumer manager
    views allocation.
    """

    consumer = models.ForeignKey(Consumer, on_delete=models.CASCADE, related_name='generator_supply_schedules')
    date = models.DateField()
    submitted_by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='generator_supply_submitted',
    )
    submitted_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['consumer', 'date'], name='unique_generator_supply_consumer_date'),
        ]

    def __str__(self) -> str:
        return f'GeneratorSupply: {self.consumer_id} on {self.date}'


class GeneratorSupplySlot(models.Model):
    schedule = models.ForeignKey(GeneratorSupplySchedule, on_delete=models.CASCADE, related_name='slots')
    slot_index = models.PositiveIntegerField()
    slot_time = models.TimeField()
    supply_mwh = models.DecimalField(max_digits=12, decimal_places=4, default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['schedule', 'slot_index'], name='unique_generator_supply_slot_index'),
        ]
        ordering = ['slot_index']

    def __str__(self) -> str:
        return f'GSupply {self.slot_index} ({self.slot_time}) = {self.supply_mwh} MWh'


class GeneratorSupplyUploadRevision(models.Model):
    """
    Records each generator Excel/manual save that changed supply vs the prior saved schedule.
    Used on the schedule revisions page to show Revision 1, 2, … with per-slot diffs.
    After CM allocation approval, new uploads may require CM review (approve / reject / override)
    with auto-approval if not resolved within a short window.
    """

    class CMReviewStatus(models.TextChoices):
        PENDING = 'pending', 'Pending CM'
        APPROVED = 'approved', 'Approved'
        AUTO_APPROVED = 'auto_approved', 'Auto-approved'
        REJECTED = 'rejected', 'Rejected'
        OVERRIDDEN = 'overridden', 'Overridden'

    schedule = models.ForeignKey(GeneratorSupplySchedule, on_delete=models.CASCADE, related_name='upload_revisions')
    revision_number = models.PositiveSmallIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='generator_supply_upload_revisions',
    )
    cm_review_status = models.CharField(
        max_length=24,
        choices=CMReviewStatus.choices,
        default=CMReviewStatus.APPROVED,
    )
    deadline_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='generator_supply_upload_revisions_resolved',
    )

    class Meta:
        ordering = ['revision_number']
        constraints = [
            models.UniqueConstraint(fields=['schedule', 'revision_number'], name='unique_generator_supply_upload_rev_no'),
        ]

    def __str__(self) -> str:
        return f'UploadRevision #{self.revision_number} schedule={self.schedule_id}'


class GeneratorSupplyUploadRevisionDelta(models.Model):
    revision = models.ForeignKey(GeneratorSupplyUploadRevision, on_delete=models.CASCADE, related_name='deltas')
    slot_index = models.PositiveIntegerField()
    previous_mwh = models.DecimalField(max_digits=12, decimal_places=4)
    new_mwh = models.DecimalField(max_digits=12, decimal_places=4)

    class Meta:
        ordering = ['slot_index']
        constraints = [
            models.UniqueConstraint(fields=['revision', 'slot_index'], name='unique_upload_revision_delta_slot'),
        ]

    def __str__(self) -> str:
        return f'delta slot={self.slot_index} rev={self.revision_id}'


class ConsumerGeneratorAllocationRun(models.Model):
    """
    Tracks an AI-generated allocation recommendation run for a consumer+date.
    The consumer manager can approve it (optionally with AI overrides).
    """

    class Status(models.TextChoices):
        SUGGESTED = 'SUGGESTED'
        APPROVED = 'APPROVED'

    consumer = models.ForeignKey(Consumer, on_delete=models.CASCADE, related_name='allocation_runs')
    date = models.DateField()
    created_by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='allocation_runs_created',
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.SUGGESTED)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['consumer', 'date'], name='unique_consumer_allocation_run_date'),
        ]

    def __str__(self) -> str:
        return f'AllocationRun consumer={self.consumer_id} date={self.date} status={self.status}'


class ConsumerGeneratorAllocationOverride(models.Model):
    """
    Stores consumer-manager overrides for the AI part only (45% portion).
    For MVP we allow overriding the AI total for the day per plant.
    """

    class OverrideType(models.TextChoices):
        AI_TOTAL = 'AI_TOTAL'  # only AI portion override for the day

    run = models.ForeignKey(ConsumerGeneratorAllocationRun, on_delete=models.CASCADE, related_name='plant_overrides')
    plant = models.ForeignKey(Plant, on_delete=models.CASCADE, related_name='allocation_overrides')
    override_type = models.CharField(max_length=16, choices=OverrideType.choices, default=OverrideType.AI_TOTAL)
    ai_alloc_mwh_override_total = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['run', 'plant'], name='unique_run_plant_ai_override'),
        ]

    def __str__(self) -> str:
        return f'Override run={self.run_id} plant={self.plant_id} ai_total={self.ai_alloc_mwh_override_total}'


class SlotAllocationApproval(models.Model):
    """
    Final slot-level allocation per plant after consumer manager approves (AI or manual override).
    plant=None with allocated_mwh=0 marks the slot as fully sold via IEX (no plant AI allocation).
    """

    consumer = models.ForeignKey(Consumer, on_delete=models.CASCADE, related_name='slot_allocation_approvals')
    date = models.DateField()
    slot_index = models.PositiveIntegerField()
    plant = models.ForeignKey(Plant, on_delete=models.CASCADE, null=True, blank=True, related_name='slot_allocation_approvals')
    allocated_mwh = models.FloatField(default=0)
    is_manual_override = models.BooleanField(default=False)
    # Stores submitted | revision1–3 (legacy) | upload_<n> for generator upload revision snapshot.
    approved_revision = models.CharField(max_length=32, default='revision2')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['consumer', 'date', 'slot_index']),
        ]

    def __str__(self) -> str:
        return f'SlotApproval c={self.consumer_id} {self.date} s={self.slot_index} p={self.plant_id} {self.allocated_mwh}'


class IexGreenDayAheadMcpSlot(models.Model):
    """
    Stores IEX Green Day-Ahead Market MCP (Rs/MWh) for each 15-min slot.
    Global market data (not consumer-specific).
    """

    date = models.DateField()
    slot_index = models.PositiveIntegerField()
    slot_time = models.TimeField()
    mcp_rs_per_mwh = models.DecimalField(max_digits=14, decimal_places=4)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['date', 'slot_index'], name='unique_iex_mcp_date_slot_index'),
        ]
        indexes = [models.Index(fields=['date'])]

    def __str__(self) -> str:
        return f'IEX MCP {self.date} slot={self.slot_index} {self.mcp_rs_per_mwh}'

