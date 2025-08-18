# business/utils/reviews_bg.py
import os, sys, json, time, threading, subprocess
from pathlib import Path
from typing import Optional, Dict
from django.conf import settings

# ------ Tunables ------
SCRAPER_STEPS = [40, 120, 200]  # progressive targets
MAX_CONCURRENT_SCRAPES = int(os.getenv("SCRAPER_MAX_CONCURRENCY", "1"))
SCRAPER_SOFT_TIMEOUT = int(os.getenv("SCRAPER_TIMEOUT_SEC", "240"))

# ------ Paths ------
BASE_DIR = Path(settings.BASE_DIR)
REVIEWS_ROOT = BASE_DIR / "var" / "reviews"

# You already have these in your project; keep using the same signatures:
def dish_csv_path(place_id: str) -> Path:
    return REVIEWS_ROOT / place_id / "dish_mentions.csv"

def place_dir(place_id: str) -> Path:
    return REVIEWS_ROOT / place_id

def progress_path(place_id: str) -> Path:
    return place_dir(place_id) / "progress.json"

def inprogress_flag(place_id: str) -> Path:
    return place_dir(place_id) / ".scrape_inprogress"

# ------ In-process locks & concurrency gate ------
_GLOBAL_SCRAPE_SEM = threading.Semaphore(MAX_CONCURRENT_SCRAPES)
_PLACE_LOCKS: Dict[str, threading.Lock] = {}
_LOCKS_LOCK = threading.Lock()

def _lock_for(place_id: str) -> threading.Lock:
    with _LOCKS_LOCK:
        if place_id not in _PLACE_LOCKS:
            _PLACE_LOCKS[place_id] = threading.Lock()
        return _PLACE_LOCKS[place_id]

# ------ Progress helpers ------
def _read_progress(place_id: str) -> Dict:
    p = progress_path(place_id)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}
    return {}

def _write_progress(place_id: str, done: int) -> None:
    p = progress_path(place_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {"done": int(done), "ts": int(time.time())}
    p.write_text(json.dumps(data))

def _next_target(done: int) -> Optional[int]:
    for t in SCRAPER_STEPS:
        if done < t:
            return t
    return None  # finished all steps

# ------ Subprocess runner (keeps gunicorn workers small) ------
def _run_scraper_subprocess(place_id: str, target: int, append: bool = True) -> None:
    """
    Runs:  python manage.py scrape_reviews --place_id ... --target N [--append]
    Your management command already exists; we only add --target/--append support (see below).
    """
    cmd = [sys.executable, "manage.py", "scrape_reviews", "--place_id", place_id, "--target", str(target)]
    if append:
        cmd.append("--append")

    env = os.environ.copy()
    # Keep Playwright headless & friendly to small instances.
    env.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/var/www/.cache/ms-playwright")
    env.setdefault("PLAYWRIGHT_NO_SANDBOX", "1")
    env.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")   # quiet TensorFlow

    subprocess.run(
        cmd,
        cwd=str(BASE_DIR),
        env=env,
        check=False,
        timeout=SCRAPER_SOFT_TIMEOUT,  # don't let a single scrape sit forever
    )

# ------ Public API you call from your view ------
def kickoff_background_refresh(place_id: str) -> None:
    """Start (or continue) backfill in a daemon thread if not already in progress."""
    def _worker():
        # only allow limited global concurrency
        if not _GLOBAL_SCRAPE_SEM.acquire(blocking=False):
            return
        try:
            lock = _lock_for(place_id)
            if not lock.acquire(blocking=False):
                return  # someone already working on this place
            try:
                pdir = place_dir(place_id)
                pdir.mkdir(parents=True, exist_ok=True)

                flag = inprogress_flag(place_id)
                if flag.exists():
                    # Another process/thread already marked it
                    return

                try:
                    flag.write_text(str(int(time.time())))
                    prog = _read_progress(place_id)
                    done = int(prog.get("done", 0))
                    target = _next_target(done)
                    if target is None:
                        return  # fully backfilled

                    _run_scraper_subprocess(place_id, target, append=True)
                    # After scraper returns, mark progress up to target.
                    _write_progress(place_id, done=target)

                finally:
                    try:
                        flag.unlink()
                    except FileNotFoundError:
                        pass

            finally:
                try:
                    lock.release()
                except Exception:
                    pass
        finally:
            _GLOBAL_SCRAPE_SEM.release()

    threading.Thread(target=_worker, daemon=True).start()

def is_stale(csv_path: Path, max_age_sec: int = 3600) -> bool:
    try:
        mtime = csv_path.stat().st_mtime
        return (time.time() - mtime) > max_age_sec
    except Exception:
        return True

def csv_ready_or_kickoff(place_id: str) -> bool:
    """
    Returns True if we already have a CSV for this place_id.
    If not, start background step 1 and return False immediately.
    """
    p = dish_csv_path(place_id)
    if p.exists():
        return True
    kickoff_background_refresh(place_id)
    return False
