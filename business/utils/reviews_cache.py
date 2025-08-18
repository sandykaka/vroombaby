# reviews_cache.py
from __future__ import annotations
import json, os, subprocess, time
import sys
from datetime import datetime
from pathlib import Path
from django.conf import settings

REVIEWS_DIR = Path(getattr(settings, "REVIEWS_CACHE_DIR",
                           Path(settings.BASE_DIR) / "var" / "reviews"))

FAST_TARGET = 40
FAST_TIME_BUDGET = 12           # keeps first response snappy
BACKFILL_TARGET = 220
BACKFILL_TIME_BUDGET = 45
STALE_TTL_SECS = 15 * 60        # if CSV older than this, treat as partial

def place_dir(place_id: str) -> Path:
    return REVIEWS_DIR / place_id

def dish_csv_path(place_id: str) -> Path:
    return place_dir(place_id) / "dish_mentions.csv"

def reviews_json_path(place_id: str) -> Path:
    return place_dir(place_id) / "reviews.json"

def _lock_path(place_id: str) -> Path:
    return place_dir(place_id) / ".refresh.lock"

def _atomic_lock(lock_path: Path) -> bool:
    """
    Returns True if we created the lock, False if it already existed.
    The lock file contains JSON with pid and ts.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        # stale lock cleanup (older than 1 hour)
        try:
            if time.time() - lock_path.stat().st_mtime > 3600:
                lock_path.unlink(missing_ok=True)
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            else:
                return False
        except Exception:
            return False
    try:
        os.write(fd, json.dumps({"pid": os.getpid(), "ts": time.time()}).encode("utf-8"))
    finally:
        os.close(fd)
    return True

def _unlock(lock_path: Path) -> None:
    try:
        lock_path.unlink(missing_ok=True)
    except Exception:
        pass

def is_stale(csv_path: Path) -> bool:
    """Stale if missing, tiny, or old."""
    if not csv_path.exists():
        return True
    try:
        df_rows = sum(1 for _ in open(csv_path, "r", encoding="utf-8")) - 1  # minus header
    except Exception:
        df_rows = 0
    if df_rows < 8:  # skinny → likely missing tabs
        return True
    age = datetime.now().timestamp() - csv_path.stat().st_mtime
    return age > 24 * 7 * 3600


def generate_csv_blocking(place_id: str, fast: bool = True) -> None:
    """Run your management command synchronously using the current interpreter."""
    manage = Path(settings.BASE_DIR) / "manage.py"
    cmd = [sys.executable, str(manage), "scrape_reviews", "--place_id", place_id]
    if fast:
        cmd.append("--fast")
    env = os.environ.copy()
    subprocess.check_call(cmd, cwd=str(settings.BASE_DIR), env=env)


def kickoff_background_refresh(place_id: str, fast: bool = False) -> None:
    """Fire-and-forget backfill using current interpreter (sys.executable)."""
    pd = Path(getattr(settings, "REVIEWS_CACHE_DIR",
                      Path(settings.BASE_DIR) / "var" / "reviews")) / place_id
    pd.mkdir(parents=True, exist_ok=True)
    lock = pd / ".refresh.lock"

    # simple 10-min lock
    try:
        if lock.exists() and (datetime.now().timestamp() - lock.stat().st_mtime) < 600:
            return
        lock.write_text(datetime.now().isoformat(), encoding="utf-8")
    except Exception:
        pass

    manage = Path(settings.BASE_DIR) / "manage.py"
    cmd = [sys.executable, str(manage), "scrape_reviews", "--place_id", place_id]
    if fast:
        cmd.append("--fast")

    env = os.environ.copy()
    log_path = pd / "refresh.log"
    with open(log_path, "ab", buffering=0) as log:
        subprocess.Popen(
            cmd,
            cwd=str(settings.BASE_DIR),
            env=env,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )

def ensure_csv_async(place_id: str) -> None:
    kickoff_background_refresh(place_id, fast=False)  # real backfill