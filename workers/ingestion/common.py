import logging

from shared.db import SessionLocal
from shared.models import RawMention

logger = logging.getLogger(__name__)


def save_mentions(source: str, rows: list[dict]) -> int:
    """rows: [{'source_url': str|None, 'content': str}]. Dedupes on source_url
    (or exact content when url is missing). Returns count inserted."""
    db = SessionLocal()
    inserted = 0
    try:
        for row in rows:
            content = (row.get("content") or "").strip()
            if not content:
                continue
            url = row.get("source_url")
            exists = db.query(RawMention.id)
            if url:
                exists = exists.filter(RawMention.source_url == url)
            else:
                exists = exists.filter(RawMention.source == source, RawMention.content == content)
            if exists.first():
                continue
            db.add(RawMention(source=source, source_url=url, content=content[:20000]))
            inserted += 1
        db.commit()
        logger.info("[%s] inserted %d new raw mentions", source, inserted)
        return inserted
    finally:
        db.close()
