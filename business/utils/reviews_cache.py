# business/reviews_cache.py
from __future__ import annotations
import os
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timedelta

from django.conf import settings
from django.core.management import call_command

# -------- tuning knobs --------
TTL = timedelta(days=7)        # how long the CSV is considered “fresh”
FAST_TARGET = 40               # quick pass for cold-cache
FAST_BUDGET = 12               # seconds the quick pass is allowed to run
FULL_TARGET = 200              # background pass goal
LOCK_MAX_AGE = timedelta(minutes=20)  # background lock auto-expires

# -------- paths --------
def _reviews_dir(place_id: str) -> Path:
    base = Path(getattr(settings, "REVIEWS_CACHE_DIR",
                        Path(settings.BASE_DIR) / "var" / "reviews"))
    return base / place_id

def dish_csv_path(place_id: str) -> Path:
    return _reviews_dir(place_id) / "dish_mentions.csv"

def _lock_path(place_id: str) -> Path:
    return _reviews_dir(place_id) / ".refresh.lock"

def _manage_py() -> Path:
    return Path(settings.BASE_DIR) / "manage.py"

# -------- freshness --------
def is_stale(p: Path) -> bool:
    try:
        age = datetime.now().timestamp() - p.stat().st_mtime
        return age > TTL.total_seconds()
    except FileNotFoundError:
        return True

# -------- background refresh --------
def _lock_is_active(lock: Path) -> bool:
    """Return True if a recent lock file exists (i.e., refresh likely still running)."""
    if not lock.exists():
        return False
    try:
        age = datetime.now().timestamp() - lock.stat().st_mtime
        return age < LOCK_MAX_AGE.total_seconds()
    except FileNotFoundError:
        return False

def _touch_lock(lock: Path) -> None:
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock.touch(exist_ok=True)
    except Exception:
        pass

def kickoff_background_refresh(place_id: str) -> None:
    """Spawn the scraper in the same venv with Playwright env set."""
    proj_root = Path(settings.BASE_DIR)
    env = os.environ.copy()
    env["PLAYWRIGHT_BROWSERS_PATH"] = os.getenv(
        "PLAYWRIGHT_BROWSERS_PATH",
        "/home/ubuntu/.cache/ms-playwright"  # <-- keep consistent with systemd
    )
    env["PLAYWRIGHT_NO_SANDBOX"] = "1"

    # simple per-place lock to avoid duplicate scrapes
    lock = dish_csv_path(place_id).parent / ".refresh.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists():
        return
    lock.write_text(str(os.getpid()))

    subprocess.Popen(
        [sys.executable, str(proj_root / "manage.py"), "scrape_reviews", "-p", place_id],
        cwd=str(proj_root),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    # scraper will remove the lock when it finishes
# Backwards-compat name used by your view:
def ensure_csv_async(place_id: str) -> None:
    kickoff_background_refresh(place_id)

# -------- cold-cache generator (blocking) --------
def generate_csv_blocking(place_id: str, fast: bool = True) -> Path:
    """
    Synchronous scrape for the very first request so you can return something quickly.
    - fast=True  → quick pass (FAST_TARGET within FAST_BUDGET seconds)
    - fast=False → full pass
    Returns the expected dish_mentions.csv path.
    """
    outdir = _reviews_dir(place_id)
    outdir.mkdir(parents=True, exist_ok=True)

    args = ["scrape_reviews", "--place_id", place_id]
    if fast:
        # Your management command should interpret these as a short pass.
        args += ["--target", str(FAST_TARGET), "--time_budget", str(FAST_BUDGET)]
    else:
        args += ["--target", str(FULL_TARGET), "--time_budget", "0"]

    # Run in-process so first response isn’t empty.
    call_command(*args)
    return outdir / "dish_mentions.csv"
