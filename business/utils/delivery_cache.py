# delivery_cache.py
from __future__ import annotations
import json, os, time, uuid
import logging
from pathlib import Path
from django.conf import settings

logger = logging.getLogger(__name__)

# Where delivery store IDs are cached (similar to reviews)
DELIVERY_DIR = Path(
    getattr(settings, "DELIVERY_CACHE_DIR", Path(settings.BASE_DIR) / "var" / "delivery")
).resolve()

# Job queue for store ID lookups
DELIVERY_QUEUE_DIR = Path(
    getattr(settings, "DELIVERY_QUEUE_DIR", Path(settings.BASE_DIR) / "var" / "delivery_queue")
).resolve()

# Make sure both exist
DELIVERY_DIR.mkdir(parents=True, exist_ok=True)
DELIVERY_QUEUE_DIR.mkdir(parents=True, exist_ok=True)

STALE_SECONDS = 30 * 24 * 60 * 60    # 30 days (delivery info changes less frequently)
LOCK_STALE_S = 10 * 60               # 10 minutes
DEDUP_TTL_S = 2 * 60                 # 2 minutes

def place_delivery_dir(place_id: str) -> Path:
    """Get directory for a specific place's delivery data"""
    d = DELIVERY_DIR / place_id
    d.mkdir(parents=True, exist_ok=True)
    return d

def delivery_info_path(place_id: str) -> Path:
    """Path to delivery store IDs JSON file"""
    return place_delivery_dir(place_id) / "store_ids.json"

def delivery_lock_path(place_id: str) -> Path:
    """Path to lock file for this place"""
    return place_delivery_dir(place_id) / ".lock"

def is_delivery_stale(place_id: str) -> bool:
    """Check if delivery data is stale and needs updating"""
    info_path = delivery_info_path(place_id)
    if not info_path.exists():
        return True

    age = time.time() - info_path.stat().st_mtime
    return age > STALE_SECONDS

def is_delivery_locked(place_id: str) -> bool:
    """Check if delivery lookup is currently in progress"""
    lock_path = delivery_lock_path(place_id)
    if not lock_path.exists():
        return False

    # Check if lock is stale
    age = time.time() - lock_path.stat().st_mtime
    if age > LOCK_STALE_S:
        try:
            lock_path.unlink(missing_ok=True)
        except:
            pass
        return False

    return True

def get_cached_delivery_info(place_id: str) -> dict | None:
    """Get cached delivery store IDs"""
    info_path = delivery_info_path(place_id)
    if not info_path.exists():
        return None

    try:
        data = json.loads(info_path.read_text())
        return data
    except (json.JSONDecodeError, FileNotFoundError):
        return None

def cache_delivery_info(place_id: str, data: dict):
    """Cache delivery store IDs"""
    info_path = delivery_info_path(place_id)

    # Add timestamp
    data["cached_at"] = time.time()
    data["place_id"] = place_id

    try:
        info_path.write_text(json.dumps(data, indent=2))
        logger.info(f"Cached delivery info for {place_id}")
    except Exception as e:
        logger.error(f"Failed to cache delivery info for {place_id}: {e}")

def create_delivery_lock(place_id: str):
    """Create lock file to prevent duplicate lookups"""
    lock_path = delivery_lock_path(place_id)
    try:
        lock_path.write_text(str(time.time()))
    except Exception as e:
        logger.error(f"Failed to create delivery lock for {place_id}: {e}")

def remove_delivery_lock(place_id: str):
    """Remove lock file"""
    lock_path = delivery_lock_path(place_id)
    try:
        lock_path.unlink(missing_ok=True)
    except Exception:
        pass

def queue_delivery_lookup(place_id: str, restaurant_name: str, address: str):
    """Queue a delivery store ID lookup job"""
    if is_delivery_locked(place_id):
        logger.info(f"Delivery lookup already in progress for {place_id}")
        return

    # Create lock
    create_delivery_lock(place_id)

    # Create job
    job_id = str(uuid.uuid4())[:8]
    job_path = DELIVERY_QUEUE_DIR / f"{job_id}_{place_id}.json"

    job_data = {
        "job_id": job_id,
        "place_id": place_id,
        "restaurant_name": restaurant_name,
        "address": address,
        "ts": time.time(),
        "type": "delivery_lookup"
    }

    try:
        job_path.write_text(json.dumps(job_data, indent=2))
        logger.info(f"Queued delivery lookup for {place_id} (job: {job_id})")
    except Exception as e:
        logger.error(f"Failed to queue delivery lookup for {place_id}: {e}")
        remove_delivery_lock(place_id)

def list_delivery_jobs() -> list:
    """List all pending delivery lookup jobs"""
    jobs = []
    for job_path in DELIVERY_QUEUE_DIR.glob("*.json"):
        try:
            job_data = json.loads(job_path.read_text())
            job_data["job_file"] = job_path
            jobs.append(job_data)
        except (json.JSONDecodeError, FileNotFoundError):
            # Clean up invalid job files
            try:
                job_path.unlink(missing_ok=True)
            except:
                pass

    # Sort by timestamp
    return sorted(jobs, key=lambda x: x.get("ts", 0))

def remove_delivery_job(job_file: Path):
    """Remove completed job file"""
    try:
        job_file.unlink(missing_ok=True)
    except Exception as e:
        logger.error(f"Failed to remove job file {job_file}: {e}")