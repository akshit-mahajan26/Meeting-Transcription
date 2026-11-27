"""Background ingestion failures: the error type, a user-safe reason, and recording a FAILED meeting."""

import logging
from datetime import timedelta

from sqlalchemy import func

from ..core.database import MeetingModel, SessionLocal
from .failure_kinds import KINDS, reason_text
from .fireflies_client import FirefliesAPIError, classify_fireflies_error
from .ownership import claim_project, get_owned_meeting

logger = logging.getLogger(__name__)


class PipelineOrchestratorError(Exception):
    """Background ingestion orchestration failed; `kind` is the failure_kinds key."""

    def __init__(self, message: str, kind: str = "unknown"):
        super().__init__(message)
        self.kind = kind


class EmptyTranscript(Exception):
    """The transcript has no sentence with text; nothing is sent to the engine. Carries the content hash."""

    def __init__(self, message: str, content_hash: str | None = None):
        super().__init__(message)
        self.content_hash = content_hash


class FirefliesDisconnected(Exception):
    """The owner has no usable linked Fireflies key for a meeting that needs one. `kind` is disconnected or key_unreadable."""

    def __init__(self, message: str, kind: str = "disconnected"):
        super().__init__(message)
        self.kind = kind


_FIREFLIES_KINDS = {
    "not_found": "not_found", "no_access": "no_access", "key_rejected": "key_rejected",
    "account_problem": "fireflies_account", "rate_limited": "rate_limited",
    "client_bug": "fireflies_invalid_request", "temporary": "fireflies_unavailable",
}


def failure_kind_for(exc: BaseException) -> tuple[str, str | None]:
    """The one failure kind (failure_kinds key) and optional detail for an ingestion exception."""
    if isinstance(exc, FirefliesAPIError):
        key = _FIREFLIES_KINDS.get(classify_fireflies_error(exc), "fireflies_unavailable")
        detail = None
        if key == "fireflies_account" and exc.friendly:
            first = exc.details[0] if isinstance(exc.details, list) and exc.details and isinstance(exc.details[0], dict) else {}
            detail = str(first.get("message") or exc.message or "") or None
        return key, detail
    if isinstance(exc, FirefliesDisconnected | PipelineOrchestratorError):
        return (exc.kind if exc.kind in KINDS else "unknown"), None
    if isinstance(exc, EmptyTranscript):
        return "no_transcript", None
    if isinstance(exc, TimeoutError):  # asyncio.TimeoutError is TimeoutError
        return "timeout", None
    return "unknown", type(exc).__name__


def friendly_failure_reason(exc: BaseException) -> str:
    """User-facing reason chosen by error type; raw exceptions can carry internal URLs, so they stay in the log."""
    return reason_text(*failure_kind_for(exc))


RETRY_WINDOW = timedelta(days=7)


def record_failure(user_id: str, meeting_id: str, project_id: str | None, title: str | None, exc: Exception,
                   source: str = "link") -> None:
    """
    Leave a visible FAILED meeting (failure_kind plus its reason in `summary`) so a background failure is not silent.
    Retryable kinds get retry_until = now() + 7 days only if it had none; permanent kinds clear it (no automatic
    retry). no_transcript also stores the content hash so a sync can tell the content did not change.
    A meeting already PROCESSED is never touched. Never raises: recording must not mask the original error.
    """
    key, detail = failure_kind_for(exc)
    reason = reason_text(key, detail)
    retryable = KINDS[key].retryable
    content_hash = getattr(exc, "content_hash", None) if key == "no_transcript" else None
    new_retry_until = func.now() + RETRY_WINDOW
    db = SessionLocal()
    try:
        meeting = get_owned_meeting(db, user_id, meeting_id)
        if meeting is not None and meeting.status == "PROCESSED":
            return
        if project_id:
            project_id = claim_project(db, user_id, project_id).id
        if meeting is None:
            db.add(MeetingModel(
                id=meeting_id, user_id=user_id, project_id=project_id, title=title,
                summary=reason, status="FAILED", source=source, failure_kind=key, content_hash=content_hash,
                retry_until=new_retry_until if retryable else None, updated_at=func.now(),
            ))
        else:
            meeting.summary = reason
            meeting.status = "FAILED"
            meeting.failure_kind = key
            if content_hash:
                meeting.content_hash = content_hash
            if not retryable:
                meeting.retry_until = None
            elif meeting.retry_until is None:
                meeting.retry_until = new_retry_until
            meeting.updated_at = func.now()
        db.commit()
    except Exception:
        db.rollback()
        logger.exception(f"Could not record ingestion failure for meeting '{meeting_id}'")
    finally:
        db.close()
