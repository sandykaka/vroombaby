# reviews_cache.py
from __future__ import annotations
import json, os, subprocess, time
import sys
from datetime import datetime
from pathlib import Path
from django.conf import settings

REVIEWS_DIR = Path(getattr(settings, "REVIEWS_CACHE_DIR",
                           Path(settings.BASE_DIR) / "var" / "reviews"))

FAST_TARGET = 30
FAST_TIME_BUDGET = 12           # keeps first response snappy
BACKFILL_TARGET = 220
BACKFILL_TIME_BUDGET = 45
STALE_TTL_S = 6 * 3600        # if CSV older than this, treat as partial
FULL_TARGET   = getattr(settings, "REVIEWS_TARGET",       200)
FULL_BUDGET   = getattr(settings, "REVIEWS_FULL_BUDGET",   90)
LOCK_STALE_S  = getattr(settings, "REVIEWS_LOCK_STALE_S", 900)  # 15 min
COOLDOWN_FULL_S    = 15 * 60     # don’t launch FULL more often than this

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

def _atomic_lock(lock_path: Path, ttl_s: int) -> bool:
    """
    Try to create a lock atomically. Return True if we acquired it.
    If an existing lock is fresh (< ttl_s), return False.
    If stale, remove and retry once.
    """
    now = time.time()
    # existing?
    if lock_path.exists():
        try:
            if now - lock_path.stat().st_mtime < ttl_s:
                return False
            # stale – remove
            lock_path.unlink(missing_ok=True)
        except Exception:
            # if we can’t stat/unlink, fall through and try EXCL create
            pass

    fd = None
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.write(fd, str(os.getpid()).encode("utf-8"))
        return True
    except FileExistsError:
        # someone else won the race
        return False
    finally:
        if fd is not None:
            os.close(fd)

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

def _reviews_count(place_id: str) -> int:
    p = reviews_json_path(place_id)
    if not p.exists():
        return 0
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return len(data) if isinstance(data, list) else 0
    except Exception:
        return 0

def has_enough_reviews(place_id: str, target: int) -> bool:
    return _reviews_count(place_id) >= int(target)


def generate_csv_blocking(place_id: str, fast: bool = True) -> None:
    """Run your management command synchronously using the current interpreter."""
    manage = Path(settings.BASE_DIR) / "manage.py"
    cmd = [sys.executable, str(manage), "scrape_reviews", "--place_id", place_id]
    if fast:
        cmd.append("--fast")
    env = os.environ.copy()
    subprocess.check_call(cmd, cwd=str(settings.BASE_DIR), env=env)



def ensure_csv_async(place_id: str, fast: bool = False) -> bool:
    """
    Spawn manage.py scrape_reviews in the background for this place.
    - Uses an atomic lock (.refresh.lock) to prevent concurrent runs
    - Uses a cooldown file (.cooldown_full) to avoid repeated FULL launches
    Returns True if a job was started, False otherwise.
    """
    d = place_dir(place_id)
    lock = d / ".refresh.lock"
    cooldown = d / ".cooldown_full"
    log_path = d / "scrape.log"

    # FULL cooldown guard
    if not fast and cooldown.exists():
        try:
            if time.time() - cooldown.stat().st_mtime < COOLDOWN_FULL_S:
                print("Cooldown active for %s; skipping FULL launch", place_id)
                return False
        except Exception:
            pass

    # atomic lock guard
    if not _atomic_lock(lock, LOCK_STALE_S):
        print("Lock present for %s; skipping spawn (fast=%s)", place_id, fast)
        return False

    # build command
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

    # mark cooldown immediately for FULL (so rapid polls don’t stack jobs)
    if not fast:
        try:
            cooldown.touch()
        except Exception:
            pass

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
