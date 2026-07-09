"""Single-process entry point for free-tier hosting (Render, Koyeb, etc.).

Runs the FastAPI API (with Telegram webhook) + RQ worker + scheduler
in one process using background threads. This avoids needing 3 separate
paid services.

Usage: python serve.py
"""

import logging
import os
import threading
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def run_worker():
    """RQ worker loop in a background thread."""
    import redis
    from rq import SimpleWorker, Queue
    from shared.config import settings

    logger.info("worker thread started")
    conn = redis.from_url(settings.REDIS_URL)
    queues = [Queue("checks", connection=conn)]
    worker = SimpleWorker(queues, connection=conn)
    worker.work()


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
    # Run migrations first
    logger.info("running database migrations...")
    os.system("alembic --config api/alembic.ini upgrade head")

    # Start worker and scheduler as daemon threads
    worker_thread = threading.Thread(target=run_worker, daemon=True)
    worker_thread.start()

    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()

    # Run API in main thread (blocks)
    run_api()
