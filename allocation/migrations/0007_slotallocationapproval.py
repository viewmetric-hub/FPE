# Generated manually

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('allocation', '0006_add_generator_schedule_approval'),
        ('core', '0004_add_hourly_tariff_difference'),
    ]

    operations = [
        migrations.CreateModel(
            name='SlotAllocationApproval',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('date', models.DateField()),
                ('slot_index', models.PositiveIntegerField()),
                ('allocated_mwh', models.FloatField(default=0)),
                ('is_manual_override', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('consumer', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='slot_allocation_approvals', to='core.consumer')),
                ('plant', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='slot_allocation_approvals', to='core.plant')),
            ],
        ),
        migrations.AddIndex(
            model_name='slotallocationapproval',
            index=models.Index(fields=['consumer', 'date', 'slot_index'], name='allocation_s_consumer_2b5c0e_idx'),
        ),
    ]
