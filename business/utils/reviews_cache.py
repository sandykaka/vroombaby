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
FULL_TARGET   = getattr(settings, "REVIEWS_TARGET",       200)
FULL_BUDGET   = getattr(settings, "REVIEWS_FULL_BUDGET",   90)
LOCK_STALE_S  = getattr(settings, "REVIEWS_LOCK_STALE_S", 900)  # 15 min

def place_dir(place_id: str) -> Path:
    base = Path(getattr(settings, "REVIEWS_CACHE_DIR",
                        Path(settings.BASE_DIR) / "var" / "reviews"))
    d = base / place_id
    d.mkdir(parents=True, exist_ok=True)
    return d

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
    try:
        return (time.time() - csv_path.stat().st_mtime) > 3600
    except FileNotFoundError:
        return True


def generate_csv_blocking(place_id: str, fast: bool = True) -> None:
    """Run your management command synchronously using the current interpreter."""
    manage = Path(settings.BASE_DIR) / "manage.py"
    cmd = [sys.executable, str(manage), "scrape_reviews", "--place_id", place_id]
    if fast:
        cmd.append("--fast")
    env = os.environ.copy()
    subprocess.check_call(cmd, cwd=str(settings.BASE_DIR), env=env)



def ensure_csv_async(place_id: str, fast: bool = False) -> bool:
    """Spawn manage.py scrape_reviews in the background.
       Returns True if a job was started, False if a fresh lock existed."""
    d = place_dir(place_id)
    lock = d / ".refresh.lock"

    # Respect a fresh lock
    if lock.exists():
        try:
            if time.time() - lock.stat().st_mtime < LOCK_STALE_S:
                return False
        except Exception:
            pass
        # stale lock — remove it
        try: lock.unlink()
        except Exception: pass

    # create/refresh the lock
    lock.write_text(str(os.getpid()))

    log_path = d / "scrape.log"
    cmd = [
        sys.executable,
        str(Path(settings.BASE_DIR) / "manage.py"),
        "scrape_reviews",
        "-p", place_id,
    ]
    if fast:
        cmd += ["--fast"]
    else:
        cmd += ["--target", str(FULL_TARGET), "--time-budget", str(FULL_BUDGET)]

    env = os.environ.copy()
    env.setdefault("DJANGO_SETTINGS_MODULE", env.get("DJANGO_SETTINGS_MODULE", ""))

    with open(log_path, "a", buffering=1) as out:
        out.write(f"\n[{time.strftime('%F %T')}] start: {'FAST' if fast else 'FULL'} -> {' '.join(cmd)}\n")
        subprocess.Popen(
            cmd,
            cwd=str(settings.BASE_DIR),
            env=env,
            stdout=out,
            stderr=subprocess.STDOUT,
            close_fds=True,
        )
    return True