# business/management/commands/scrape_worker.py
from django.core.management.base import BaseCommand
from django.conf import settings

from pathlib import Path
import os, sys, time, json, logging, subprocess

from business.utils.reviews_cache import place_dir, pick_next, list_jobs

logger = logging.getLogger(__name__)

# Defaults (override in settings if you like)
BASE_DIR        = Path(getattr(settings, "BASE_DIR"))
DEFAULT_QDIR    = Path(getattr(settings, "QUEUE_DIR", BASE_DIR / "var" / "queue"))
DEFAULT_RDIR    = Path(getattr(settings, "REVIEWS_CACHE_DIR", BASE_DIR / "var" / "reviews"))

LOCK_STALE_S    = int(getattr(settings, "LOCK_STALE_S", 8 * 60))     # lock freshness
FULL_TARGET     = int(getattr(settings, "FULL_TARGET", 200))
FULL_BUDGET     = int(getattr(settings, "FULL_BUDGET", 90))

class Command(BaseCommand):
    help = "File-queue worker that runs queued scrape_reviews jobs"

    def add_arguments(self, parser):
        parser.add_argument("--queue-dir", default=str(DEFAULT_QDIR))
        parser.add_argument("--reviews-dir", default=str(DEFAULT_RDIR))
        parser.add_argument("--concurrency", type=int, default=1)   # kept for future; loop is serial
        parser.add_argument("--poll-interval", type=float, default=1.0)
        parser.add_argument("--log-level", default="INFO")


    def handle(self, *args, **opts):
        logging.getLogger().setLevel(getattr(logging, opts["log_level"].upper(), logging.INFO))
        queue_dir   = Path(opts["queue_dir"])
        reviews_dir = Path(opts["reviews_dir"])
        self.stdout.write(self.style.SUCCESS(f"🏁 scrape_worker started  queue={queue_dir}  reviews={reviews_dir}"))

        while True:
            jobs = list_jobs(queue_dir)
            if not jobs:
                time.sleep(opts["poll_interval"])
                continue

            ts, place_id, mode, target, budget, job_path = pick_next(jobs)

            d = place_dir(place_id)
            lock = d / ".refresh.lock"

            # lock gate
            if lock.exists():
                try:
                    age = time.time() - lock.stat().st_mtime
                except Exception:
                    age = 0
                if age < LOCK_STALE_S:
                    logger.info("Skip %s: fresh lock (%.1fs old).", place_id, age)
                    time.sleep(opts["poll_interval"])
                    continue
                else:
                    try: lock.unlink()
                    except Exception: pass

            # claim lock
            try:
                lock.write_text(str(os.getpid()))
            except Exception:
                pass

            # remove the job file now (we picked it)
            try: job_path.unlink()
            except Exception: pass

            # run manage.py scrape_reviews (subprocess)
            log_path = d / "scrape.log"
            cmd = [
                sys.executable,
                str(Path(os.environ.get("DJANGO_SETTINGS_MODULE") and Path.cwd() / "manage.py" or Path.cwd() / "manage.py")),
                "scrape_reviews",
                "-p", place_id,
            ]
            if mode == "fast":
                cmd += ["--fast"]
            else:
                cmd += ["--target", str(target), "--time-budget", str(budget)]

            # prettify header
            start_line = f"[{time.strftime('%F %T')}] start: {mode.upper()} -> {' '.join(cmd)}\n"
            with open(log_path, "a", buffering=1) as out:
                out.write(start_line)
                proc = subprocess.Popen(cmd, cwd=str(Path.cwd()), stdout=out, stderr=subprocess.STDOUT, close_fds=True)
                rc = proc.wait()
                out.write(f"[{time.strftime('%F %T')}] worker finished rc={rc}\n")

            try: lock.unlink()
            except Exception: pass