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
            # Update the item
            item.name = cleaned_name
            item.size = cleaned_size
            item.save(update_fields=['name', 'size'])
            stats['cleaned'] = True

            if verbose:
                self.stdout.write(f"   ✅ Updated GroceryItem ID {item.id}")

            # Update ShoppingListItems referencing this item
            list_items = ShoppingListItem.objects.filter(grocery_item=item)
            for list_item in list_items:
                list_item.name = cleaned_name
                list_item.size = cleaned_size
                list_item.save(update_fields=['name', 'size'])
                stats['list_items_updated'] += 1

            if stats['list_items_updated'] > 0 and verbose:
                self.stdout.write(f"   📊 Updated {stats['list_items_updated']} shopping list items")

            # Check for duplicates after cleanup
            stats['duplicates_merged'] = self._merge_duplicates_for_item(item, verbose)

        self.stdout.write(self.style.SUCCESS(f"   ✅ Cleaned: '{original_name}' → '{cleaned_name}'"))

        return stats

    def _merge_duplicates_for_item(self, item, verbose=False):
        """
        After cleaning, check if this item now duplicates another GroceryItem.
        If so, merge them (similar to merge_duplicate_grocery_items logic).

        Returns: count of merged duplicates
        """
        # Find other items with same name+brand+size+store
        duplicates = GroceryItem.objects.filter(
            name=item.name,
            brand=item.brand,
            size=item.size,
            store_name=item.store_name
        ).exclude(id=item.id)

        if not duplicates.exists():
            return 0

        merged_count = 0

        for duplicate in duplicates:
            # Merge purchase counts
            item.times_purchased += duplicate.times_purchased

            # Fill missing image_url
            if not item.image_url and duplicate.image_url:
                item.image_url = duplicate.image_url

            # Fill missing barcode
            if not item.barcode and duplicate.barcode:
                item.barcode = duplicate.barcode
                item.enriched_from_barcode = duplicate.enriched_from_barcode
                item.first_enriched_by = duplicate.first_enriched_by
                item.first_enriched_at = duplicate.first_enriched_at

            # Fill missing nutrition
            if not item.nutrition_data and duplicate.nutrition_data:
                item.nutrition_data = duplicate.nutrition_data
                item.nutriscore_grade = duplicate.nutriscore_grade
                item.nova_group = duplicate.nova_group
                item.last_nutrition_fetch = duplicate.last_nutrition_fetch

            # Update ShoppingListItems to point to the kept item
            ShoppingListItem.objects.filter(grocery_item=duplicate).update(grocery_item=item)

            # Delete the duplicate
            duplicate.delete()
            merged_count += 1

            if verbose:
                self.stdout.write(f"   🔗 Merged duplicate GroceryItem ID {duplicate.id}")

        # Save the merged item
        item.save()

        return merged_count

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
