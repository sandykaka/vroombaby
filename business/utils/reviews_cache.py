# business/reviews_cache.py
from __future__ import annotations
import os
from pathlib import Path
from datetime import datetime, timedelta
from django.conf import settings
from django.core.management import call_command

TTL = timedelta(days=7)  # how long a CSV is considered “fresh”

def cache_base() -> Path:
    base = Path(getattr(settings, "REVIEWS_CACHE_DIR", Path(settings.BASE_DIR) / "var" / "reviews"))
    base.mkdir(parents=True, exist_ok=True)
    return base

def place_dir(place_id: str) -> Path:
    p = cache_base() / place_id
    p.mkdir(parents=True, exist_ok=True)
    return p

def dish_csv_path(place_id: str) -> Path:
    return place_dir(place_id) / "dish_mentions.csv"

def is_stale(p: Path) -> bool:
    try:
        age = datetime.now().timestamp() - p.stat().st_mtime
        return age > TTL.total_seconds()
    except FileNotFoundError:
        return True

def ensure_csv_async(place_id: str):
    """
    Hook for background refresh (Celery/Huey/RQ). Non-blocking.
    For now, it’s a stub.
    """
    # Example with Celery:
    # from .tasks import recompute_dish_mentions
    # recompute_dish_mentions.delay(place_id)
    pass

def generate_csv_blocking(place_id: str) -> Path:
    outdir = Path(getattr(settings, "REVIEWS_CACHE_DIR",
                          Path(settings.BASE_DIR) / "var" / "reviews")) / place_id
    outdir.mkdir(parents=True, exist_ok=True)
    # call your command and tell it where to write
    call_command("scrape_reviews", place_id=place_id, out_dir=str(outdir))
    return outdir / "dish_mentions.csv"