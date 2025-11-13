# Generated manually - Add nutrition fields to GroceryItem

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shopright', '0011_productrecall_recallmatch'),
    ]

    operations = [
        migrations.AddField(
            model_name='groceryitem',
            name='nutriscore_grade',
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text='Nutri-Score grade: A (best) to E (worst)',
                max_length=1
            ),
        ),
        migrations.AddField(
            model_name='groceryitem',
            name='nova_group',
            field=models.IntegerField(
                blank=True,
                help_text='NOVA processing level: 1 (unprocessed) to 4 (ultra-processed)',
                null=True
            ),
        ),
        migrations.AddField(
            model_name='groceryitem',
            name='nutrition_data',
            field=models.JSONField(
                blank=True,
                help_text='Full nutritional breakdown: sugar, sodium, calories, etc.',
                null=True
            ),
        ),
        migrations.AddField(
            model_name='groceryitem',
            name='last_nutrition_fetch',
            field=models.DateTimeField(
                blank=True,
                help_text='When nutrition data was last fetched (for cache invalidation)',
                null=True
            ),
        ),
    ]
