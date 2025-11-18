"""
Django management command to clean up product names containing size information.

Usage:
    python manage.py clean_product_names --dry-run
    python manage.py clean_product_names --store "Trader Joe's"
    python manage.py clean_product_names --verbose

This command fixes data quality issues where AI receipt parsing included size
information in product names instead of extracting it to the size field.

Example issues:
    - "Raspberries 12 oz" should be name="Raspberries", size="12 oz"
    - "Strawberries Org 1 lb" should be name="Strawberries Org", size="1 lb"

The command will:
1. Find GroceryItem records where name contains size patterns
2. Extract size from name and update both fields
3. Update ShoppingListItem records that reference the cleaned GroceryItems
4. Merge any resulting duplicates (same name+brand+size+store after cleanup)

Author: Claude Code
Date: 2025-01-18
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count
from shopright.models import GroceryItem, ShoppingListItem
from shopright.utils.product_cleanup import clean_product_name_and_size, should_extract_size
from collections import defaultdict
import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Clean up product names containing size information'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Preview changes without modifying database',
        )
        parser.add_argument(
            '--store',
            type=str,
            help='Only process items from specific store (e.g., "Trader Joe\'s")',
        )
        parser.add_argument(
            '--verbose',
            action='store_true',
            help='Show detailed logging for each cleanup operation',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        store_filter = options['store']
        verbose = options['verbose']

        # Set logging level
        if verbose:
            logger.setLevel(logging.DEBUG)

        # Display mode
        mode = "DRY-RUN MODE" if dry_run else "LIVE MODE"
        self.stdout.write(self.style.WARNING(f"\n{'='*60}"))
        self.stdout.write(self.style.WARNING(f"  CLEAN PRODUCT NAMES - {mode}"))
        self.stdout.write(self.style.WARNING(f"{'='*60}\n"))

        if not dry_run:
            self.stdout.write(self.style.ERROR("⚠️  WARNING: This will modify your database!"))
            self.stdout.write(self.style.ERROR("   Make sure you have a backup before proceeding.\n"))
            confirm = input("Type 'yes' to continue: ")
            if confirm.lower() != 'yes':
                self.stdout.write(self.style.WARNING("Aborted."))
                return

        # Statistics
        stats = {
            'items_analyzed': 0,
            'items_cleaned': 0,
            'list_items_updated': 0,
            'duplicates_merged': 0,
            'errors': 0,
        }

        # Find items needing cleanup
        self.stdout.write("\n🔍 Finding items with size in name...")
        items_to_clean = self._find_items_needing_cleanup(store_filter)

        if not items_to_clean:
            self.stdout.write(self.style.SUCCESS("\n✅ No items found needing cleanup!"))
            return

        self.stdout.write(self.style.SUCCESS(f"   Found {len(items_to_clean)} items needing cleanup\n"))

        # Process each item
        for idx, item in enumerate(items_to_clean, 1):
            try:
                stats['items_analyzed'] += 1

                self.stdout.write(f"\n[{idx}/{len(items_to_clean)}] Processing: {item.name} @ {item.store_name}")

                if verbose:
                    self.stdout.write(f"   Current: name='{item.name}', size='{item.size}'")

                # Clean the item
                item_stats = self._process_item(item, dry_run=dry_run, verbose=verbose)

                # Update statistics
                if item_stats['cleaned']:
                    stats['items_cleaned'] += 1
                stats['list_items_updated'] += item_stats['list_items_updated']
                stats['duplicates_merged'] += item_stats['duplicates_merged']

            except Exception as e:
                stats['errors'] += 1
                self.stdout.write(self.style.ERROR(f"   ❌ Error: {str(e)}"))
                logger.error(f"Failed to process item {item.id}: {e}", exc_info=True)

        # Display final statistics
        self._display_statistics(stats, dry_run)

    def _find_items_needing_cleanup(self, store_filter=None):
        """
        Find GroceryItem records where name likely contains size patterns.

        Returns: QuerySet of GroceryItem records
        """
        query = GroceryItem.objects.all()

        if store_filter:
            query = query.filter(store_name__iexact=store_filter)

        # Filter items where name contains size patterns
        items = []
        for item in query:
            if should_extract_size(item.name, item.size):
                items.append(item)

        return items

    def _process_item(self, item, dry_run=False, verbose=False):
        """
        Process a single GroceryItem: clean name/size, update references, merge duplicates.

        Returns: dict with statistics for this item
        """
        stats = {
            'cleaned': False,
            'list_items_updated': 0,
            'duplicates_merged': 0,
        }

        # Extract size from name
        original_name = item.name
        original_size = item.size
        cleaned_name, cleaned_size = clean_product_name_and_size(original_name, original_size)

        # Check if cleanup actually changed anything
        if cleaned_name == original_name and cleaned_size == original_size:
            if verbose:
                self.stdout.write("   No changes needed")
            return stats

        if verbose:
            self.stdout.write(f"   Cleaned: name='{cleaned_name}', size='{cleaned_size}'")

        if dry_run:
            stats['cleaned'] = True
            # Count ShoppingListItems that would be updated
            stats['list_items_updated'] = ShoppingListItem.objects.filter(
                grocery_item=item
            ).count()
            return stats

        # Execute cleanup with transaction
        with transaction.atomic():
            # IMPORTANT: Check for duplicates BEFORE saving to avoid IntegrityError
            # If cleaned values would create a duplicate, merge into existing item instead
            existing_duplicate = GroceryItem.objects.filter(
                name=cleaned_name,
                brand=item.brand,
                size=cleaned_size,
                store_name=item.store_name
            ).exclude(id=item.id).first()

            if existing_duplicate:
                # Duplicate already exists! Merge this item into it instead of updating
                if verbose:
                    self.stdout.write(f"   🔗 Found existing item (ID {existing_duplicate.id}), merging...")

                # Merge purchase counts
                existing_duplicate.times_purchased += item.times_purchased

                # Copy over any missing data from the item being cleaned
                if not existing_duplicate.image_url and item.image_url:
                    existing_duplicate.image_url = item.image_url
                if not existing_duplicate.barcode and item.barcode:
                    existing_duplicate.barcode = item.barcode
                    existing_duplicate.enriched_from_barcode = item.enriched_from_barcode
                    existing_duplicate.first_enriched_by = item.first_enriched_by
                    existing_duplicate.first_enriched_at = item.first_enriched_at
                if not existing_duplicate.nutrition_data and item.nutrition_data:
                    existing_duplicate.nutrition_data = item.nutrition_data
                    existing_duplicate.nutriscore_grade = item.nutriscore_grade
                    existing_duplicate.nova_group = item.nova_group
                    existing_duplicate.last_nutrition_fetch = item.last_nutrition_fetch

                existing_duplicate.save()

                # Point all ShoppingListItems to the existing duplicate
                # CAREFUL: Updating name/size might create duplicates in shopping lists
                list_items = ShoppingListItem.objects.filter(grocery_item=item)
                for list_item in list_items:
                    # Check if this shopping list already has an item with cleaned name/brand/size
                    existing_list_item = ShoppingListItem.objects.filter(
                        shopping_list=list_item.shopping_list,
                        name=cleaned_name,
                        brand=list_item.brand,
                        size=cleaned_size
                    ).exclude(id=list_item.id).first()

                    if existing_list_item:
                        # Merge: add quantities and keep most recent purchase date
                        existing_list_item.quantity += list_item.quantity
                        existing_list_item.purchase_count += list_item.purchase_count
                        if list_item.last_purchased_date:
                            if not existing_list_item.last_purchased_date or list_item.last_purchased_date > existing_list_item.last_purchased_date:
                                existing_list_item.last_purchased_date = list_item.last_purchased_date
                        existing_list_item.save()
                        # Delete the duplicate list item
                        list_item.delete()
                        if verbose:
                            self.stdout.write(f"      🔗 Merged duplicate shopping list item")
                    else:
                        # Safe to update
                        list_item.grocery_item = existing_duplicate
                        list_item.name = cleaned_name
                        list_item.size = cleaned_size
                        list_item.save(update_fields=['grocery_item', 'name', 'size'])

                    stats['list_items_updated'] += 1

                # Delete the now-redundant item
                item.delete()
                stats['duplicates_merged'] = 1
                stats['cleaned'] = True

                if verbose:
                    self.stdout.write(f"   ✅ Merged into existing item ID {existing_duplicate.id}")
            else:
                # No duplicate exists, safe to update in place
                item.name = cleaned_name
                item.size = cleaned_size
                item.save(update_fields=['name', 'size'])
                stats['cleaned'] = True

                if verbose:
                    self.stdout.write(f"   ✅ Updated GroceryItem ID {item.id}")

                # Update ShoppingListItems referencing this item
                # CAREFUL: Updating name/size might create duplicates in shopping lists
                list_items = ShoppingListItem.objects.filter(grocery_item=item)
                for list_item in list_items:
                    # Check if this shopping list already has an item with cleaned name/brand/size
                    existing_list_item = ShoppingListItem.objects.filter(
                        shopping_list=list_item.shopping_list,
                        name=cleaned_name,
                        brand=list_item.brand,
                        size=cleaned_size
                    ).exclude(id=list_item.id).first()

                    if existing_list_item:
                        # Merge: add quantities and keep most recent purchase date
                        existing_list_item.quantity += list_item.quantity
                        existing_list_item.purchase_count += list_item.purchase_count
                        if list_item.last_purchased_date:
                            if not existing_list_item.last_purchased_date or list_item.last_purchased_date > existing_list_item.last_purchased_date:
                                existing_list_item.last_purchased_date = list_item.last_purchased_date
                        existing_list_item.save()
                        # Delete the duplicate list item
                        list_item.delete()
                        if verbose:
                            self.stdout.write(f"      🔗 Merged duplicate shopping list item")
                    else:
                        # Safe to update
                        list_item.name = cleaned_name
                        list_item.size = cleaned_size
                        list_item.save(update_fields=['name', 'size'])

                    stats['list_items_updated'] += 1

                if stats['list_items_updated'] > 0 and verbose:
                    self.stdout.write(f"   📊 Updated {stats['list_items_updated']} shopping list items")

        self.stdout.write(self.style.SUCCESS(f"   ✅ Cleaned: '{original_name}' → '{cleaned_name}'"))

        return stats

    def _display_statistics(self, stats, dry_run):
        """Display final statistics summary."""
        mode = "WOULD HAVE" if dry_run else "ACTUAL"

        self.stdout.write(self.style.WARNING(f"\n{'='*60}"))
        self.stdout.write(self.style.WARNING(f"  {mode} RESULTS"))
        self.stdout.write(self.style.WARNING(f"{'='*60}\n"))

        self.stdout.write(f"Items analyzed:                {stats['items_analyzed']}")
        self.stdout.write(f"Items cleaned:                 {stats['items_cleaned']}")
        self.stdout.write(f"ShoppingListItems updated:     {stats['list_items_updated']}")
        self.stdout.write(f"Duplicates merged:             {stats['duplicates_merged']}")

        if stats['errors'] > 0:
            self.stdout.write(self.style.ERROR(f"Errors encountered:            {stats['errors']}"))

        if dry_run:
            self.stdout.write(self.style.SUCCESS("\n✅ Dry-run complete. No changes were made to the database."))
            self.stdout.write(self.style.WARNING("   Run without --dry-run to apply changes."))
        else:
            self.stdout.write(self.style.SUCCESS("\n✅ Cleanup complete!"))

        self.stdout.write("")  # Empty line
