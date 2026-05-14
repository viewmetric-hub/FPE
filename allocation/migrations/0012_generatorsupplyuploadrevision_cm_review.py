from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('allocation', '0011_generator_supply_upload_revision'),
    ]

    operations = [
        migrations.AddField(
            model_name='generatorsupplyuploadrevision',
            name='cm_review_status',
            field=models.CharField(
                choices=[
                    ('pending', 'Pending CM'),
                    ('approved', 'Approved'),
                    ('auto_approved', 'Auto-approved'),
                    ('rejected', 'Rejected'),
                    ('overridden', 'Overridden'),
                ],
                default='approved',
                max_length=24,
            ),
        ),
        migrations.AddField(
            model_name='generatorsupplyuploadrevision',
            name='deadline_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='generatorsupplyuploadrevision',
            name='resolved_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='generatorsupplyuploadrevision',
            name='resolved_by',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='generator_supply_upload_revisions_resolved',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
