# business/reviews_cache.py
from __future__ import annotations
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta
from subprocess import Popen

from django.conf import settings
from django.core.management import call_command

TTL = timedelta(days=7)  # how long a CSV is considered “fresh”
FAST_TARGET = 40
FAST_BUDGET = 12
FULL_TARGET = 200

def _manage_py() -> str:
    # path to manage.py
    return str(Path(settings.BASE_DIR) / "manage.py")

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
    Fire-and-forget full scrape (200 reviews). Uses the same venv/python
    as the running process. Safe to call even if one is already running.
    """
    py = sys.executable
    env = os.environ.copy()
    # Make sure Playwright uses the same browser cache path as your service:
    env.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""))
    env.setdefault("PLAYWRIGHT_NO_SANDBOX", "1")
    Popen(
        [py, _manage_py(), "scrape_reviews", "--place_id", place_id,
         "--target", str(FULL_TARGET), "--time_budget", "0"],
        cwd=str(Path(settings.BASE_DIR)),
        env=env,
        stdout=open(os.devnull, "wb"),
        stderr=open(os.devnull, "wb"),
    )

def generate_csv_blocking(place_id: str, fast:bool = True) -> Path:
    outdir = Path(getattr(settings, "REVIEWS_CACHE_DIR",
                          Path(settings.BASE_DIR) / "var" / "reviews")) / place_id
    outdir.mkdir(parents=True, exist_ok=True)
    # call your command and tell it where to write
    args = ["scrape_reviews", "--place_id", place_id]
    if fast:
        args.append("--fast")
    else:
        args += ["--target", str(FULL_TARGET), "--time_budget", "0"]
    call_command(*args)
    return outdir / "dish_mentions.csv"