"""Background ingestion failures: the error type, a user-safe reason, and recording a FAILED meeting."""

import logging
from datetime import timedelta

from sqlalchemy import func

from ..core.database import MeetingModel, SessionLocal
from .fireflies_client import FirefliesAPIError, classify_fireflies_error
from .ownership import claim_project, get_owned_meeting

logger = logging.getLogger(__name__)


class PipelineOrchestratorError(Exception):
    """Exception raised when background ingestion orchestration fails."""


class FirefliesDisconnected(Exception):
    """The owner has no usable linked Fireflies key for a meeting that needs one."""


def friendly_failure_reason(exc: Exception) -> str:
    """User-facing reason for a failed ingestion. Raw exceptions can carry internal URLs and response bodies,
    so only a message chosen by error type is shown; the full exception is in the server log."""
    if isinstance(exc, FirefliesAPIError):
        kind = classify_fireflies_error(exc)
        if kind == "not_found":
            return "This transcript was not found in Fireflies. Check the link and try again."
        if kind == "no_access":
            return "This Fireflies account cannot access this transcript. It may belong to someone else, or access was removed."
        if kind == "key_rejected":
            return "Fireflies rejected the API key. Contact your administrator."
        if kind == "account_problem":
            # Fireflies' own text (without our "GraphQL Error from Fireflies:" prefix), only when Fireflies marks it safe to show
            first = exc.details[0] if isinstance(exc.details, list) and exc.details and isinstance(exc.details[0], dict) else {}
            if exc.friendly and (first.get("message") or exc.message):
                return str(first.get("message") or exc.message)
            return "Fireflies refused the request because of an account or plan restriction."
        if kind == "rate_limited":
            return "Fireflies rate limit reached. Try again in a few minutes."
        if kind == "temporary":
            return "Could not reach Fireflies. Try again in a few minutes."
        return "Fireflies could not return this transcript. Try again later."
    if isinstance(exc, FirefliesDisconnected):
        return str(exc)
    if isinstance(exc, PipelineOrchestratorError):
        return "Processing the transcript failed. Try again in a few minutes."
    return "Unexpected error while ingesting this meeting. Try again, and contact support if it persists."


SKIPPED_SUMMARY = (
    "Skipped: Fireflies has no transcript with this ID that this account can access. It may have been deleted, "
    "access may have been removed, or the link may be wrong. It will not be retried automatically."
)


def record_failure(user_id: str, meeting_id: str, project_id: str | None, title: str | None, exc: Exception,
                   source: str = "link", skipped: bool | None = None) -> None:
    """
    Leave a visible FAILED meeting (reason in `summary`) so a background failure is not silent.
    Skipped (retry_until NULL, no automatic retry) when `skipped` is True, or when None and Fireflies has no such
    transcript. A failed meeting gets retry_until = now() + 7 days only if it had none. updated_at is always set.
    A meeting already PROCESSED is never touched. Never raises: recording must not mask the original error.
    """
    if skipped is None:
        skipped = isinstance(exc, FirefliesAPIError) and classify_fireflies_error(exc) == "not_found"
    reason = SKIPPED_SUMMARY if skipped else friendly_failure_reason(exc)
    new_retry_until = func.now() + timedelta(days=7)
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
                summary=reason, status="FAILED", source=source,
                retry_until=None if skipped else new_retry_until, updated_at=func.now(),
            ))
        else:
            meeting.summary = reason
            meeting.status = "FAILED"
            if skipped:
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
