from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0002_userprofile_logo'),
    ]

    operations = [
        migrations.AddField(
            model_name='userprofile',
            name='plantwise_timeslot_variation_count',
            field=models.PositiveSmallIntegerField(default=7),
        ),
    ]
