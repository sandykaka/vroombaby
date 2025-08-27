# reviews_cache.py
from __future__ import annotations
import json, os, subprocess, time
import logging
import sys
from pathlib import Path
from django.conf import settings
logger = logging.getLogger(__name__)


# Where per-place CSVs/JSONs live
REVIEWS_DIR = Path(
    getattr(settings, "REVIEWS_CACHE_DIR", Path(settings.BASE_DIR) / "var" / "reviews")
).resolve()

# Where scrape jobs (.json) are dropped for the worker to consume
# Best practice: keep the queue OUTSIDE the reviews dir
QUEUE_DIR = Path(
    getattr(settings, "REVIEWS_QUEUE_DIR", Path(settings.BASE_DIR) / "var" / "queue")
).resolve()

# Make sure both exist
REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
QUEUE_DIR.mkdir(parents=True, exist_ok=True)
FAST_TARGET   = 40
FAST_BUDGET   = 15
FULL_TARGET   = 200
FULL_BUDGET   = 90

STALE_SECONDS = 7 * 24 * 60 * 60    # 7 days
LOCK_STALE_S  = 20 * 60             # 20 minutes
DEDUP_TTL_S   = 15 * 60             # 15 minutes

def place_dir(place_id: str) -> Path:

    d = REVIEWS_DIR / place_id
    d.mkdir(parents=True, exist_ok=True)
    return d

def list_jobs(queue_dir: Path):
    jobs = []
    for p in queue_dir.glob("*.json"):
        try:
            job = json.loads(p.read_text())
            ts  = float(job.get("ts") or p.stat().st_mtime)
            pid = job["place_id"]
            mode = job.get("mode","fast")
            target = int(job.get("target") or (40 if mode == "fast" else FULL_TARGET))
            budget = int(job.get("budget") or (15 if mode == "fast" else FULL_BUDGET))
            jobs.append((ts, pid, mode, target, budget, p))
        except Exception:
            continue
    # oldest first
    return sorted(jobs, key=lambda t: t[0])

def pick_next(jobs):
    if not jobs:
        return None
    fasts = [j for j in jobs if j[2] == "fast"]
    return min(fasts, key=lambda t: t[0]) if fasts else jobs[0]

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


def generate_csv_blocking(place_id: str, fast: bool = True) -> None:
    """Run your management command synchronously using the current interpreter."""
    manage = Path(settings.BASE_DIR) / "manage.py"
    cmd = [sys.executable, str(manage), "scrape_reviews", "--place_id", place_id]
    if fast:
        cmd.append("--fast")
    env = os.environ.copy()
    subprocess.check_call(cmd, cwd=str(settings.BASE_DIR), env=env)


def enqueue_scrape_job(place_id: str, mode: str, target: int, budget: int, queue_dir: Path = QUEUE_DIR):
    queue_dir.mkdir(parents=True, exist_ok=True)
    job = {
        "place_id": place_id,
        "mode": mode,                # "fast" or "full"
        "ts": time.time(),
        "target": int(target),
        "budget": int(budget),
    }
    # Always .json
    fname = f"{place_id}.{mode}.{int(job['ts'])}.json"
    tmp = queue_dir / (fname + ".tmp")
    final = queue_dir / fname
    tmp.write_text(json.dumps(job), encoding="utf-8")
    os.replace(tmp, final)          # atomic move
    return final

def _has_recent_job(place_id: str, queue_dir: Path, ttl_s: int) -> bool:
    now = time.time()
    for p in queue_dir.glob(f"{place_id}.*.json"):
        try:
            if now - p.stat().st_mtime < ttl_s:
                return True
        except Exception:
            pass
    return False

def ensure_csv_async(place_id: str, fast: bool, queue_dir = QUEUE_DIR, dedupe_ttl_s: int = DEDUP_TTL_S) -> bool:
    """
    Enqueue a scrape job. We do NOT touch the .refresh.lock here.
    We dedupe by scanning the queue for recent jobs for the same place_id.
    """
    if _has_recent_job(place_id, queue_dir, dedupe_ttl_s):
        logger.info("🧊 queue-dedupe %s: recent job exists; skipping enqueue", place_id)
        return False

    mode   = "fast" if fast else "full"
    target = 40 if fast else FULL_TARGET
    budget = 15 if fast else FULL_BUDGET
    job_path = enqueue_scrape_job(place_id, mode=mode, target=target, budget=budget, queue_dir=queue_dir)
    logger.info("📥 ENQUEUED %s %s → %s", mode.upper(), place_id, job_path.name)
    return True