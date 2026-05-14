from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0004_add_hourly_tariff_difference'),
    ]

    operations = [
        migrations.AddField(
            model_name='plant',
            name='max_consumption_per_day',
            field=models.DecimalField(decimal_places=4, default=0, max_digits=12),
        ),
    ]
