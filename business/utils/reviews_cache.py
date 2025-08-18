# reviews_cache.py
from __future__ import annotations
import json, os, subprocess, time
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
    """
    'Partial' signal for the app:
    - CSV missing, or
    - CSV older than TTL, or
    - reviews.json exists but has < BACKFILL_TARGET/2 reviews (very early state)
    """
    if not csv_path.exists():
        return True
    try:
        if (time.time() - csv_path.stat().st_mtime) > STALE_TTL_SECS:
            return True
    except Exception:
        return True

    try:
        rj = csv_path.parent / "reviews.json"
        if rj.exists():
            n = 0
            with rj.open("r", encoding="utf-8") as f:
                # cheap line count: each review on its own line when pretty-printed
                # (OK if not exact — just a heuristic)
                for _ in f:
                    n += 1
            if n < (BACKFILL_TARGET // 2):
                return True
    except Exception:
        pass
    return False

def generate_csv_blocking(place_id: str) -> None:
    """
    Tiny, blocking first pass to guarantee a quick first response.
    If a background job is already running (lock present), don't start another.
    """
    pdir = place_dir(place_id)
    pdir.mkdir(parents=True, exist_ok=True)

    lock = _lock_path(place_id)
    if not _atomic_lock(lock):
        # someone else is already building; just return and let the request read what's there
        return
    try:
        cmd = [
            str(Path(settings.BASE_DIR) / "manage.py"),
            "scrape_reviews",
            "--place_id", place_id,
            "--target", str(FAST_TARGET),
            "--time-budget", str(FAST_TIME_BUDGET),
            "--fast",
            "--append",
        ]
        env = os.environ.copy()
        # make sure playwright uses the system path you installed to
        env.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/var/www/.cache/ms-playwright")
        env.setdefault("PLAYWRIGHT_NO_SANDBOX", "1")

        # run via the project venv's python
        py = Path(settings.BASE_DIR) / ".venv_ethnicolr" / "bin" / "python"
        subprocess.run([str(py)] + cmd, cwd=str(Path(settings.BASE_DIR)),
                       env=env, check=False, timeout=FAST_TIME_BUDGET + 30)
    finally:
        _unlock(lock)

def ensure_csv_async(place_id: str) -> None:
    """
    Queues a low-priority backfill. Only one job per place at a time.
    If lock exists → no-op. We do not wait.
    """
    lock = _lock_path(place_id)
    if not _atomic_lock(lock):
        return

    cmd = [
        str(Path(settings.BASE_DIR) / "manage.py"),
        "scrape_reviews",
        "--place_id", place_id,
        "--target", str(BACKFILL_TARGET),
        "--time-budget", str(BACKFILL_TIME_BUDGET),
        "--append",
    ]
    env = os.environ.copy()
    env.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/var/www/.cache/ms-playwright")
    env.setdefault("PLAYWRIGHT_NO_SANDBOX", "1")
    py = Path(settings.BASE_DIR) / ".venv_ethnicolr" / "bin" / "python"

    # lower priority so it doesn't starve gunicorn
    nice = ["nice", "-n", "10", "ionice", "-c", "3"]
    subprocess.Popen(nice + [str(py)] + cmd,
                     cwd=str(Path(settings.BASE_DIR)), env=env,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # lock will be cleared by the management command at the end (see below)
