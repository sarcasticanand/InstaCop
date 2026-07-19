"""Single-process entry point for free-tier hosting (Render, Koyeb, etc.).

Runs the FastAPI API (with Telegram webhook) + RQ worker + scheduler
in one process using background threads. This avoids needing 3 separate
paid services.

Usage: python serve.py
"""

import logging
import os
import subprocess
import sys
import threading
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _fix_database_url():
    """Supabase gives postgresql:// URLs which trigger psycopg2. We use psycopg
    (v3) which needs postgresql+psycopg:// scheme."""
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("postgresql://") and "+psycopg" not in url:
        os.environ["DATABASE_URL"] = url.replace("postgresql://", "postgresql+psycopg://", 1)


def run_worker():
    """RQ worker loop in a background thread using SimpleWorker with
    signal handling disabled (signals only work in main thread)."""
    import redis
    from rq import Queue, SimpleWorker
    from rq.timeouts import TimerDeathPenalty
    from shared.config import settings

    class ThreadWorker(SimpleWorker):
        # SIGALRM-based job timeouts don't work outside the main thread;
        # TimerDeathPenalty is RQ's thread-safe alternative.
        death_penalty_class = TimerDeathPenalty

    logger.info("worker thread started")
    conn = redis.from_url(settings.REDIS_URL)
    queues = [Queue("checks", connection=conn)]
    worker = ThreadWorker(queues, connection=conn)
    # Disable signal handlers since we're in a thread
    worker._install_signal_handlers = lambda: None
    worker.work()


def _maybe_monthly_sweep():
    """Once a month, harvest new posts from the configured subreddits so the
    brand database keeps growing without anyone asking. Last-run marker lives
    in Redis (survives restarts)."""
    import redis
    from shared.config import settings

    r = redis.from_url(settings.REDIS_URL)
    last = float(r.get("sweep:last_run") or 0)
    if time.time() - last < 30 * 86400:
        return
    from bot.service import get_queue
    from workers.ingestion.reddit_brand import load_subreddits

    for sub in load_subreddits():
        get_queue().enqueue("workers.jobs.reddit_sweep_job", sub, 45, job_timeout=1800)
    r.set("sweep:last_run", time.time())
    logger.info("monthly reddit sweep queued for %d subreddits", len(load_subreddits()))


def run_scheduler():
    """Scheduler loop in a background thread."""
    from workers.jobs import recheck_stale_public_pages, send_due_followups

    logger.info("scheduler thread started")
    tick = 0
    while True:
        try:
            sent = send_due_followups()
            if sent:
                logger.info("sent %d followups", sent)
        except Exception as exc:
            logger.error("followup tick failed: %s", exc)

        if tick % (60 * 24) == 0 and tick > 0:
            try:
                recheck_stale_public_pages()
            except Exception as exc:
                logger.error("staleness pass failed: %s", exc)
            try:
                _maybe_monthly_sweep()
            except Exception as exc:
                logger.error("monthly sweep check failed: %s", exc)
            try:
                from shared.meta_token import maybe_refresh_meta_token

                maybe_refresh_meta_token()
            except Exception as exc:
                logger.error("meta token refresh failed: %s", exc)
            try:
                from engine.operator_graph import autopopulate_from_payment_identities
                from shared.db import SessionLocal

                db = SessionLocal()
                try:
                    autopopulate_from_payment_identities(db)
                finally:
                    db.close()
            except Exception as exc:
                logger.error("operator-link pass failed: %s", exc)

        tick += 1
        time.sleep(60)


def run_api():
    """FastAPI server (main thread)."""
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("api.app.main:app", host="0.0.0.0", port=port)


if __name__ == "__main__":
    _fix_database_url()

    # Run migrations (from the api/ directory where alembic.ini lives)
    logger.info("running database migrations...")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "--config", "api/alembic.ini", "upgrade", "head"],
        cwd=os.path.dirname(os.path.abspath(__file__)),
    )
    if result.returncode != 0:
        logger.error("migration failed with code %d", result.returncode)

    # Start worker and scheduler as daemon threads
    worker_thread = threading.Thread(target=run_worker, daemon=True)
    worker_thread.start()

    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()

    # Run API in main thread (blocks)
    run_api()
