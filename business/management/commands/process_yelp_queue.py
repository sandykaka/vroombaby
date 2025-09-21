"""
Management command for processing nightly Yelp jobs
Processes place_ids from the queue and scrapes Yelp reviews
"""

import logging
import time
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

# Configure logger for cleaner cron output
logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = "Process pending place_ids for Yelp review scraping (run nightly)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--max-places", 
            type=int, 
            default=10,
            help="Maximum number of places to process in one run"
        )
        parser.add_argument(
            "--target-reviews",
            type=int,
            default=150,
            help="Target number of Yelp reviews to scrape per place"
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be processed without actually doing it"
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Show detailed processing messages"
        )

    def handle(self, *args, **options):
        max_places = options["max_places"]
        target_reviews = options["target_reviews"]
        dry_run = options["dry_run"]
        verbose = options["verbose"]
        
        # Set log level based on verbose flag
        if not verbose:
            # Suppress verbose scraping logs for cleaner cron output
            logging.getLogger('business.utils.yelp_integration').setLevel(logging.WARNING)
        
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        if dry_run:
            self.stdout.write(f"[{timestamp}] 🧪 DRY RUN MODE - No actual processing")
        
        self.stdout.write(f"[{timestamp}] 🌙 Starting nightly Yelp processing (max: {max_places} places, target: {target_reviews} reviews each)")
        
        try:
            from business.utils.yelp_queue import (
                get_pending_place_ids, 
                remove_from_pending_queue, 
                mark_as_processed,
                get_queue_stats
            )
            from business.utils.yelp_integration import scrape_yelp_from_place_id
        except ImportError as e:
            self.stdout.write(f"[{timestamp}] ❌ Failed to import required modules: {e}")
            return
        
        # Show queue stats
        stats = get_queue_stats()
        self.stdout.write(f"[{timestamp}] 📊 Queue stats: {stats['pending_count']} pending, {stats['processed_last_24h']} processed in last 24h")
        
        # Get pending place_ids
        pending_place_ids = get_pending_place_ids()
        
        if not pending_place_ids:
            self.stdout.write(f"[{timestamp}] ✅ No pending place_ids to process")
            return
        
        # Process up to max_places
        to_process = pending_place_ids[:max_places]
        self.stdout.write(f"[{timestamp}] 🎯 Processing {len(to_process)} place_ids: {to_process}")
        
        processed_count = 0
        failed_count = 0
        
        for place_id in to_process:
            place_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            if verbose:
                self.stdout.write(f"[{place_timestamp}] 🏪 Processing place_id: {place_id}")
            
            if dry_run:
                self.stdout.write(f"[{place_timestamp}] 🧪 Would scrape Yelp for {place_id}")
                continue
            
            try:
                # Scrape Yelp reviews for this place_id
                result = scrape_yelp_from_place_id(
                    place_id=place_id,
                    target_reviews=target_reviews
                )
                
                if result and result.get('reviews'):
                    review_count = len(result['reviews'])
                    restaurant_name = result.get('restaurant_name', 'Unknown')
                    
                    self.stdout.write(f"[{place_timestamp}] ✅ Scraped {review_count} reviews for {restaurant_name}")
                    processed_count += 1
                    
                    # Mark as processed and remove from queue
                    mark_as_processed(place_id)
                    remove_from_pending_queue(place_id)
                    
                else:
                    self.stdout.write(f"[{place_timestamp}] ⚠️ No reviews found for {place_id}")
                    # Still remove from queue to avoid infinite retries
                    remove_from_pending_queue(place_id)
                    failed_count += 1
                
            except Exception as e:
                self.stdout.write(f"[{place_timestamp}] ❌ Failed to process {place_id}: {e}")
                # Remove from queue to avoid infinite retries
                remove_from_pending_queue(place_id)
                failed_count += 1
                logger.error(f"Yelp processing failed for {place_id}: {e}")
            
            # Small delay between requests to be respectful
            if not dry_run:
                time.sleep(2)
        
        # Final summary
        final_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if not dry_run:
            self.stdout.write(f"[{final_timestamp}] 🎉 Nightly Yelp processing complete: {processed_count} successful, {failed_count} failed")
        else:
            self.stdout.write(f"[{final_timestamp}] 🧪 DRY RUN complete - would have processed {len(to_process)} places")