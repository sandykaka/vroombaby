"""
Yelp Queue Management
Simple file-based queue for tracking place_ids that need Yelp processing
"""

import logging
import os
import time
from pathlib import Path
from typing import List, Set

from django.conf import settings

logger = logging.getLogger(__name__)

# Queue directory
YELP_QUEUE_DIR = Path(
    getattr(settings, "YELP_QUEUE_DIR", Path(settings.BASE_DIR) / "var" / "yelp_queue")
)
YELP_QUEUE_DIR.mkdir(parents=True, exist_ok=True)

PENDING_FILE = YELP_QUEUE_DIR / "pending_place_ids.txt"
PROCESSED_FILE = YELP_QUEUE_DIR / "processed_place_ids.txt"


def add_place_id_to_queue(place_id: str) -> bool:
    """
    Add a place_id to the pending queue for nightly Yelp processing.
    Returns True if added, False if already exists.
    """
    if not place_id or not place_id.strip():
        return False
    
    place_id = place_id.strip()
    
    # Check if already processed recently (within last 7 days)
    if is_recently_processed(place_id, days=7):
        logger.info(f"Place {place_id} was processed recently, skipping queue add")
        return False
    
    # Check if already exists in the file
    if PENDING_FILE.exists():
        try:
            with open(PENDING_FILE, "r", encoding="utf-8") as f:
                existing_ids = [line.strip() for line in f if line.strip()]
            if place_id in existing_ids:
                logger.info(f"Place {place_id} already in pending queue")
                return False
        except Exception as e:
            logger.error(f"Error reading pending queue: {e}")
    
    # Add to pending queue
    try:
        with open(PENDING_FILE, "a", encoding="utf-8") as f:
            f.write(f"{place_id}\n")
        logger.info(f"Added place_id {place_id} to Yelp queue")
        return True
    except Exception as e:
        logger.error(f"Failed to add place_id {place_id} to queue: {e}")
        return False


def get_pending_place_ids() -> List[str]:
    """Get all pending place_ids from the queue."""
    if not PENDING_FILE.exists():
        return []
    
    try:
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            place_ids = [line.strip() for line in f if line.strip()]
        return list(set(place_ids))  # Remove duplicates
    except Exception as e:
        logger.error(f"Failed to read pending place_ids: {e}")
        return []


def is_in_pending_queue(place_id: str) -> bool:
    """Check if place_id is already in pending queue."""
    pending_ids = get_pending_place_ids()
    return place_id in pending_ids


def mark_as_processed(place_id: str) -> None:
    """Mark a place_id as processed with timestamp."""
    try:
        timestamp = int(time.time())
        with open(PROCESSED_FILE, "a", encoding="utf-8") as f:
            f.write(f"{place_id},{timestamp}\n")
        logger.info(f"Marked place_id {place_id} as processed")
    except Exception as e:
        logger.error(f"Failed to mark place_id {place_id} as processed: {e}")


def is_recently_processed(place_id: str, days: int = 7) -> bool:
    """Check if place_id was processed within the last N days."""
    if not PROCESSED_FILE.exists():
        return False
    
    cutoff_time = time.time() - (days * 24 * 60 * 60)
    
    try:
        with open(PROCESSED_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split(",")
                if len(parts) >= 2 and parts[0] == place_id:
                    try:
                        processed_time = int(parts[1])
                        if processed_time > cutoff_time:
                            return True
                    except ValueError:
                        continue
        return False
    except Exception as e:
        logger.error(f"Failed to check if place_id {place_id} was recently processed: {e}")
        return False


def remove_from_pending_queue(place_id: str) -> bool:
    """Remove a place_id from the pending queue."""
    if not PENDING_FILE.exists():
        return False
    
    try:
        # Read all pending place_ids
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
        
        # Filter out the processed place_id
        remaining = [line for line in lines if line != place_id]
        
        # Write back the remaining place_ids
        with open(PENDING_FILE, "w", encoding="utf-8") as f:
            for line in remaining:
                f.write(f"{line}\n")
        
        logger.info(f"Removed place_id {place_id} from pending queue")
        return True
    except Exception as e:
        logger.error(f"Failed to remove place_id {place_id} from pending queue: {e}")
        return False


def get_queue_stats() -> dict:
    """Get statistics about the queue."""
    pending = get_pending_place_ids()
    
    # Count processed in last 24 hours
    recent_processed = 0
    if PROCESSED_FILE.exists():
        cutoff_time = time.time() - (24 * 60 * 60)
        try:
            with open(PROCESSED_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    parts = line.strip().split(",")
                    if len(parts) >= 2:
                        try:
                            if int(parts[1]) > cutoff_time:
                                recent_processed += 1
                        except ValueError:
                            continue
        except Exception:
            pass
    
    return {
        "pending_count": len(pending),
        "processed_last_24h": recent_processed,
        "queue_file": str(PENDING_FILE),
        "processed_file": str(PROCESSED_FILE)
    }