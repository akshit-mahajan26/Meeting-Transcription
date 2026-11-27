"""Synchronous database helpers for the pipeline orchestrator (run via asyncio.to_thread)."""

import logging
from typing import Any

from ..core.database import SessionLocal
from .ownership import get_owned_meeting

logger = logging.getLogger(__name__)


def check_idempotency(user_id: str, meeting_id: str, content_hash: str, source: str) -> dict[str, Any] | None:
    """Returns the result for an unchanged PROCESSED meeting, else None.
    A link request also marks the meeting as link-owned and clears its purge date, on this early path too."""
    db = SessionLocal()
    try:
        existing = get_owned_meeting(db, user_id, meeting_id)
        if existing and source == "link" and (existing.source != "link" or existing.purge_at is not None):
            existing.source = "link"
            existing.purge_at = None
            db.commit()
        if existing and existing.content_hash == content_hash and existing.status == "PROCESSED":
            logger.info(f"Idempotency hit for meeting '{meeting_id}' (content_hash: {content_hash[:8]}). Skipping re-processing.")
            return {
                "status": "success",
                "meeting_id": meeting_id,
                "is_idempotent_hit": True,
                "title": existing.title,
                "total_turns": len(existing.turns) if existing.turns else 0,
                "total_topics": len(existing.topics) if existing.topics else 0,
            }
        return None
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
