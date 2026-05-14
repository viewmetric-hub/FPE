# Generated manually

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('allocation', '0007_slotallocationapproval'),
    ]

    operations = [
        migrations.AddField(
            model_name='slotallocationapproval',
            name='approved_revision',
            field=models.CharField(
                choices=[
                    ('submitted', 'Submitted schedule'),
                    ('revision1', 'Revision 1'),
                    ('revision2', 'Latest revision'),
                ],
                default='revision2',
                max_length=16,
            ),
        ),
    ]
