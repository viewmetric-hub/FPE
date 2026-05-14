from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('allocation', '0012_generatorsupplyuploadrevision_cm_review'),
    ]

    operations = [
        migrations.AlterField(
            model_name='slotallocationapproval',
            name='approved_revision',
            field=models.CharField(default='revision2', max_length=32),
        ),
    ]
