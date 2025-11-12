# Generated manually for store_location feature

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shopright', '0009_remove_aislelocation_contributors_locationvote'),
    ]

    operations = [
        # Remove old unique constraints
        migrations.RemoveConstraint(
            model_name='shoppinglist',
            name='unique_family_store',
        ),
        migrations.RemoveConstraint(
            model_name='shoppinglist',
            name='unique_user_store',
        ),
        # Add store_location field
        migrations.AddField(
            model_name='shoppinglist',
            name='store_location',
            field=models.CharField(blank=True, default='', max_length=200),
        ),
        # Add new unique constraints that include store_location
        migrations.AddConstraint(
            model_name='shoppinglist',
            constraint=models.UniqueConstraint(
                fields=['family', 'store_name', 'store_location'],
                condition=models.Q(family__isnull=False),
                name='unique_family_store_location'
            ),
        ),
        migrations.AddConstraint(
            model_name='shoppinglist',
            constraint=models.UniqueConstraint(
                fields=['user', 'store_name', 'store_location'],
                condition=models.Q(user__isnull=False, family__isnull=True),
                name='unique_user_store_location'
            ),
        ),
    ]
