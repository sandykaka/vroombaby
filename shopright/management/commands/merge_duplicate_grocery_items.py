"""
Django management command to merge duplicate GroceryItems and ShoppingListItems.

Usage:
    python manage.py merge_duplicate_grocery_items --dry-run
    python manage.py merge_duplicate_grocery_items --store "Trader Joe's"
    python manage.py merge_duplicate_grocery_items --verbose

This command identifies and merges duplicate GroceryItem records that were created
before the fuzzy matching fix was implemented. Duplicates occur when receipts were
parsed with incomplete brand/size data.

Example duplicates:
    - GroceryItem #1: name="Mango Chunks", brand="Trader Joe's", size="12 oz", barcode="123"
    - GroceryItem #2: name="Mango Chunks", brand="", size="", barcode=""

The command will:
1. Find duplicate groups (same name + store, different brand/size)
2. Select the "winner" (prioritizes barcode data, completeness, popularity)
3. Merge data (sum times_purchased, fill missing fields)
4. Update ShoppingListItem foreign keys to point to winner
5. Merge duplicate ShoppingListItems in the same list
6. Delete loser GroceryItems

Author: Claude Code
Date: 2025-11-17
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count, Q
from shopright.models import GroceryItem, ShoppingListItem
from collections import defaultdict
import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Merge duplicate GroceryItems created before fuzzy matching fix'

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
            help='Show detailed logging for each merge operation',
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
        self.stdout.write(self.style.WARNING(f"  MERGE DUPLICATE GROCERY ITEMS - {mode}"))
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
            'duplicate_groups_found': 0,
            'items_merged': 0,
            'list_items_updated': 0,
            'list_items_merged': 0,
            'total_purchases_consolidated': 0,
            'errors': 0,
        }

        # Find duplicate groups
        self.stdout.write("\n🔍 Finding duplicate groups...")
        duplicate_groups = self._find_duplicate_groups(store_filter)

        if not duplicate_groups:
            self.stdout.write(self.style.SUCCESS("\n✅ No duplicates found!"))
            return

        self.stdout.write(self.style.SUCCESS(f"   Found {len(duplicate_groups)} duplicate groups\n"))

        # Process each duplicate group
        for idx, group in enumerate(duplicate_groups, 1):
            try:
                self.stdout.write(f"\n[{idx}/{len(duplicate_groups)}] Processing: {group['name']} @ {group['store_name']}")
                self.stdout.write(f"   {group['count']} duplicates found")

                # Process this group
                group_stats = self._process_duplicate_group(
                    group,
                    dry_run=dry_run,
                    verbose=verbose
                )

                # Update statistics
                stats['duplicate_groups_found'] += 1
                stats['items_merged'] += group_stats['items_merged']
                stats['list_items_updated'] += group_stats['list_items_updated']
                stats['list_items_merged'] += group_stats['list_items_merged']
                stats['total_purchases_consolidated'] += group_stats['purchases_consolidated']

            except Exception as e:
                stats['errors'] += 1
                self.stdout.write(self.style.ERROR(f"   ❌ Error: {str(e)}"))
                logger.error(f"Failed to process group {group}: {e}", exc_info=True)

        # Display final statistics
        self._display_statistics(stats, dry_run)

    def _find_duplicate_groups(self, store_filter=None):
        """
        Find groups of GroceryItems with same name+store but different brand/size.

        Returns: [
            {
                'name': 'Mango Chunks',
                'store_name': 'Trader Joe's',
                'ids': [1, 2, 3],
                'count': 3
            },
            ...
        ]
        """
        # Group by name and store, find groups with count > 1
        query = GroceryItem.objects.values('name', 'store_name').annotate(
            item_count=Count('id')
        ).filter(item_count__gt=1)

        if store_filter:
            query = query.filter(store_name__iexact=store_filter)

        groups = []
        for group_data in query:
            item_ids = list(GroceryItem.objects.filter(
                name=group_data['name'],
                store_name=group_data['store_name']
            ).values_list('id', flat=True))

            groups.append({
                'name': group_data['name'],
                'store_name': group_data['store_name'],
                'ids': item_ids,
                'count': len(item_ids)
            })

        # Sort by count descending (process largest groups first)
        groups.sort(key=lambda x: x['count'], reverse=True)

        return groups

    def _process_duplicate_group(self, group, dry_run=False, verbose=False):
        """
        Process a single duplicate group: merge data, update references, delete losers.

        Returns: dict with statistics for this group
        """
        stats = {
            'items_merged': 0,
            'list_items_updated': 0,
            'list_items_merged': 0,
            'purchases_consolidated': 0,
        }

        # Get all duplicates in this group
        duplicates = list(GroceryItem.objects.filter(id__in=group['ids']))

        if len(duplicates) <= 1:
            return stats  # Not actually duplicates

        # Select the winner using priority hierarchy
        winner = self._select_best_item(duplicates)
        losers = [d for d in duplicates if d.id != winner.id]

        if verbose:
            self.stdout.write(f"   Winner: ID={winner.id}, brand='{winner.brand}', size='{winner.size}', barcode={bool(winner.barcode)}")
            for loser in losers:
                self.stdout.write(f"   Loser:  ID={loser.id}, brand='{loser.brand}', size='{loser.size}', barcode={bool(loser.barcode)}")

        if dry_run:
            # Just count what would happen
            stats['items_merged'] = len(losers)
            stats['purchases_consolidated'] = sum(item.times_purchased for item in duplicates)

            # Count ShoppingListItems that would be updated
            loser_ids = [loser.id for loser in losers]
            stats['list_items_updated'] = ShoppingListItem.objects.filter(
                grocery_item_id__in=loser_ids
            ).count()

            return stats

        # Execute merge with transaction (rollback on error)
        with transaction.atomic():
            # Merge data into winner
            winner, purchases_sum = self._merge_grocery_item_data(winner, losers)
            stats['purchases_consolidated'] = purchases_sum

            # Update ShoppingListItem foreign keys
            loser_ids = [loser.id for loser in losers]
            updated_count = ShoppingListItem.objects.filter(
                grocery_item_id__in=loser_ids
            ).update(grocery_item=winner)
            stats['list_items_updated'] = updated_count

            if verbose:
                self.stdout.write(f"   📊 Updated {updated_count} shopping list items")

            # Merge duplicate ShoppingListItems in same lists
            merged_count = self._merge_duplicate_list_items(winner, verbose)
            stats['list_items_merged'] = merged_count

            # Delete losers
            for loser in losers:
                loser.delete()
                stats['items_merged'] += 1

        self.stdout.write(self.style.SUCCESS(f"   ✅ Merged {stats['items_merged']} items"))

        return stats

    def _select_best_item(self, duplicates):
        """
        Select the best GroceryItem from a duplicate set.

        Priority hierarchy:
        1. Has barcode data (enriched_from_barcode=True)
        2. Has most complete data (nutrition, image, brand, size)
        3. Most popular (times_purchased)
        4. Oldest record (lowest id)

        Returns: GroceryItem (the winner)
        """
        # Prioritize items with barcode
        barcode_items = [
            item for item in duplicates
            if item.enriched_from_barcode and item.barcode
        ]

        if barcode_items:
            # If multiple have barcodes, pick most complete
            return max(barcode_items, key=lambda x: (
                bool(x.nutrition_data),
                bool(x.image_url),
                bool(x.brand),
                bool(x.size),
                x.times_purchased,
                -x.id  # Negative to prefer older (lower id)
            ))

        # No barcode - pick most complete
        return max(duplicates, key=lambda x: (
            bool(x.nutrition_data),
            bool(x.image_url),
            bool(x.brand),
            bool(x.size),
            x.times_purchased,
            -x.id  # Negative to prefer older
        ))

    def _merge_grocery_item_data(self, winner, losers):
        """
        Merge data from losers into winner.

        Returns: (updated_winner, total_purchases_sum)
        """
        # Sum times_purchased
        total_purchases = sum(item.times_purchased for item in [winner] + losers)
        winner.times_purchased = total_purchases

        # Fill missing image_url from losers
        if not winner.image_url:
            for loser in losers:
                if loser.image_url:
                    winner.image_url = loser.image_url
                    break

        # Fill missing barcode from losers
        if not winner.barcode:
            for loser in losers:
                if loser.barcode:
                    winner.barcode = loser.barcode
                    winner.enriched_from_barcode = loser.enriched_from_barcode
                    winner.first_enriched_by = loser.first_enriched_by
                    winner.first_enriched_at = loser.first_enriched_at
                    break

        # Fill missing nutrition_data from losers
        if not winner.nutrition_data:
            for loser in losers:
                if loser.nutrition_data:
                    winner.nutrition_data = loser.nutrition_data
                    winner.nutriscore_grade = loser.nutriscore_grade
                    winner.nova_group = loser.nova_group
                    winner.last_nutrition_fetch = loser.last_nutrition_fetch
                    break

        # Keep earliest enrichment timestamp
        enrichment_dates = [
            (item.first_enriched_at, item.first_enriched_by)
            for item in [winner] + losers
            if item.first_enriched_at
        ]
        if enrichment_dates:
            earliest = min(enrichment_dates, key=lambda x: x[0])
            winner.first_enriched_at = earliest[0]
            winner.first_enriched_by = earliest[1]

        # Fill missing brand/size from losers (if winner has empty values)
        if not winner.brand:
            for loser in losers:
                if loser.brand:
                    winner.brand = loser.brand
                    break

        if not winner.size:
            for loser in losers:
                if loser.size:
                    winner.size = loser.size
                    break

        winner.save()

        return winner, total_purchases

    def _merge_duplicate_list_items(self, winner_grocery_item, verbose=False):
        """
        After updating foreign keys, merge ShoppingListItems that are now duplicates
        in the same list (same name, effectively same product).

        Returns: count of merged items
        """
        # Get all list items pointing to this grocery item
        list_items = ShoppingListItem.objects.filter(
            grocery_item=winner_grocery_item
        ).select_related('shopping_list')

        # Group by (shopping_list_id, name)
        groups = defaultdict(list)
        for item in list_items:
            key = (item.shopping_list_id, item.name.lower())
            groups[key].append(item)

        merged_count = 0

        for (list_id, name), items in groups.items():
            if len(items) <= 1:
                continue  # No duplicates in this list

            # Select keeper (most complete item)
            keeper = max(items, key=lambda x: (
                bool(x.brand),
                bool(x.size),
                x.purchase_count,
                -x.id
            ))

            duplicates = [item for item in items if item.id != keeper.id]

            # Merge data
            keeper.quantity = sum(item.quantity for item in items)

            # Keep latest purchase date
            purchase_dates = [
                item.last_purchased_date
                for item in items
                if item.last_purchased_date
            ]
            if purchase_dates:
                keeper.last_purchased_date = max(purchase_dates)

            # Sum purchase counts
            keeper.purchase_count = sum(item.purchase_count for item in items)

            # Fill missing brand/size
            if not keeper.brand:
                for dup in duplicates:
                    if dup.brand:
                        keeper.brand = dup.brand
                        break

            if not keeper.size:
                for dup in duplicates:
                    if dup.size:
                        keeper.size = dup.size
                        break

            keeper.save()

            # Delete duplicates
            for dup in duplicates:
                dup.delete()
                merged_count += 1

            if verbose:
                self.stdout.write(f"   🔗 Merged {len(duplicates)} duplicate list items for '{name}'")

        return merged_count

    def _display_statistics(self, stats, dry_run):
        """Display final statistics summary."""
        mode = "WOULD HAVE" if dry_run else "ACTUAL"

        self.stdout.write(self.style.WARNING(f"\n{'='*60}"))
        self.stdout.write(self.style.WARNING(f"  {mode} RESULTS"))
        self.stdout.write(self.style.WARNING(f"{'='*60}\n"))

        self.stdout.write(f"Duplicate groups found:        {stats['duplicate_groups_found']}")
        self.stdout.write(f"GroceryItems merged:           {stats['items_merged']}")
        self.stdout.write(f"ShoppingListItems updated:     {stats['list_items_updated']}")
        self.stdout.write(f"ShoppingListItems merged:      {stats['list_items_merged']}")
        self.stdout.write(f"Total purchases consolidated:  {stats['total_purchases_consolidated']}")

        if stats['errors'] > 0:
            self.stdout.write(self.style.ERROR(f"Errors encountered:            {stats['errors']}"))

        if dry_run:
            self.stdout.write(self.style.SUCCESS("\n✅ Dry-run complete. No changes were made to the database."))
            self.stdout.write(self.style.WARNING("   Run without --dry-run to apply changes."))
        else:
            self.stdout.write(self.style.SUCCESS("\n✅ Merge complete!"))

        self.stdout.write("")  # Empty line
