# Generated manually for unique delivery subscription constraint

from django.db import migrations, models

class Migration(migrations.Migration):

    dependencies = [
        ('shopright', '0026_shoppingtrip_delivery_alter_shoppingtrip_shopper'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='deliverysubscription',
            constraint=models.UniqueConstraint(
                condition=models.Q(('status__in', ['active', 'pending_confirmation'])),
                fields=['customer'],
                name='one_active_delivery_subscription_per_customer'
            ),
        ),
    ]