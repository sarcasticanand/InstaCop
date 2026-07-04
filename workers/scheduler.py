"""Tiny in-process scheduler. Every minute: due follow-ups. Once a day:
staleness re-checks (E2) + operator-link autopopulation (C3).
Usage: python -m workers.scheduler"""

import logging
import time

from shared.observability import init_sentry

init_sentry("scheduler")

from workers.jobs import recheck_stale_public_pages, send_due_followups

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

INTERVAL_SECONDS = 60
DAILY_EVERY_TICKS = 60 * 24  # once a day at 60s ticks

if __name__ == "__main__":
    logger.info("scheduler started (followups every %ss; daily staleness/link pass)", INTERVAL_SECONDS)
    tick = 0
    while True:
        try:
            sent = send_due_followups()
            if sent:
                logger.info("sent %d followups", sent)
        except Exception as exc:
            logger.error("followup tick failed: %s", exc)

        if tick % DAILY_EVERY_TICKS == 0:
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
        time.sleep(INTERVAL_SECONDS)
