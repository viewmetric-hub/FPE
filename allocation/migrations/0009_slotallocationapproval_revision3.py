# Generated manually

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('allocation', '0008_slotallocationapproval_approved_revision'),
    ]

    operations = [
        migrations.AlterField(
            model_name='slotallocationapproval',
            name='approved_revision',
            field=models.CharField(
                choices=[
                    ('submitted', 'Submitted schedule'),
                    ('revision1', 'Revision 1'),
                    ('revision2', 'Latest revision'),
                    ('revision3', 'Revision 3'),
                ],
                default='revision2',
                max_length=16,
            ),
        ),
    ]
