from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shillak', '0012_home_is_premium_home_premium_user_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='plaiditem',
            name='needs_reauth',
            field=models.BooleanField(default=False),
        ),
    ]
