"""Background ingestion failures: the error type, a user-safe reason, and recording a FAILED meeting."""

import logging

from ..core.database import MeetingModel, SessionLocal
from .fireflies_client import FirefliesAPIError, classify_fireflies_error
from .ownership import claim_project, get_owned_meeting

logger = logging.getLogger(__name__)


class PipelineOrchestratorError(Exception):
    """Exception raised when background ingestion orchestration fails."""


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
    if isinstance(exc, PipelineOrchestratorError):
        return "Processing the transcript failed. Try again in a few minutes."
    return "Unexpected error while ingesting this meeting. Try again, and contact support if it persists."


def record_failure(user_id: str, meeting_id: str, project_id: str | None, title: str | None, exc: Exception) -> None:
    """
    Leave a visible FAILED meeting (reason in `summary`) so a background failure is not silent.
    A meeting already PROCESSED is never touched. Never raises: recording must not mask the original error.
    """
    reason = friendly_failure_reason(exc)
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
                summary=reason, status="FAILED",
            ))
        else:
            meeting.summary = reason
            meeting.status = "FAILED"
        db.commit()
    except Exception:
        db.rollback()
        logger.exception(f"Could not record ingestion failure for meeting '{meeting_id}'")
    finally:
        db.close()
