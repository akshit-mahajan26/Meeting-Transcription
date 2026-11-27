import logging
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ....api.deps import get_current_user
from ....core.database import MeetingModel, UserModel, get_db
from ....core.ratelimit import limit_by_user
from ....services.ingest_runner import ingest_runner
from ....services.ownership import claim_project, get_owned_meeting, meeting_taken

logger = logging.getLogger(__name__)

router = APIRouter()

SUPPORTED_MEDIA_EXTENSIONS = (".mp3", ".mp4", ".wav", ".m4a", ".aac", ".ogg", ".webm", ".flac")


class IngestLinkRequest(BaseModel):
    """Request body for asynchronous meeting link ingestion."""
    link: HttpUrl = Field(..., description="Fireflies dashboard view link or public audio/video file link")
    project_id: str | None = Field(default=None, description="Parent project identifier")
    title: str | None = Field(default=None, description="Optional custom title for the meeting")


class IngestLinkResponse(BaseModel):
    """Immediate HTTP 202 Accepted response payload."""
    status: str = "queued"
    already_ingested: bool = False
    job_type: str
    message: str
    meeting_identifier: str
    project_id: str | None = None
    target_link: str


def parse_meeting_link(url_str: str) -> dict[str, Any]:
    """
    Parses and categorizes an incoming meeting link.

    Returns:
        dict with:
            - 'type': 'FIREFLIES_DASHBOARD' or 'RAW_MEDIA'
            - 'id': extracted transcript ID or file URL
            - 'title': extracted title from slug (if present)
    Raises:
        ValueError if URL format is unsupported.
    """
    parsed = urlparse(url_str)
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""

    # 1. Fireflies Dashboard Link Check
    if host == "fireflies.ai" or host.endswith(".fireflies.ai"):
        if "::" in path:
            parts = path.split("::")
            slug_part = parts[0].rstrip("/").split("/")[-1]
            transcript_id = parts[1].split("?")[0].split("#")[0].strip("/")
            formatted_title = slug_part.replace("-", " ").replace("_", " ").title()
            return {"type": "FIREFLIES_DASHBOARD", "id": transcript_id, "title": formatted_title}
        match = re.search(r"/(?:view|transcripts)/([a-zA-Z0-9_\-]+)", path)
        if match:
            transcript_id = match.group(1)
            return {"type": "FIREFLIES_DASHBOARD", "id": transcript_id, "title": None}

    # 2. Raw Media File Extension Check
    lower_path = path.lower()
    if any(lower_path.endswith(ext) for ext in SUPPORTED_MEDIA_EXTENSIONS):
        return {"type": "RAW_MEDIA", "id": url_str}

    # 3. Cloud storage audio links with query params
    if any(f"{ext}?" in url_str.lower() for ext in SUPPORTED_MEDIA_EXTENSIONS):
        return {"type": "RAW_MEDIA", "id": url_str}

    raise ValueError(
        "Unsupported meeting link format. Expected a Fireflies dashboard link "
        "(e.g., https://app.fireflies.ai/view/<id>) or a direct audio/video file "
        "(.mp3, .wav, .mp4, .m4a)."
    )


def _assert_ids_available(db: Session, user_id: str, meeting_id: str) -> None:
    """Meeting IDs are globally unique, so refuse to ingest into an ID another account already owns."""
    if meeting_taken(db, user_id, meeting_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Meeting '{meeting_id}' is already in use and cannot be ingested into this account.",
        )


def _queue_meeting(db: Session, user_id: str, meeting_id: str, project_id: str | None, title: str | None) -> bool:
    """Puts the meeting on the ingestion queue in one transaction. False when it is already queued."""
    _assert_ids_available(db, user_id, meeting_id)
    try:
        # Row lock: concurrent requests for the same meeting queue it once
        meeting = get_owned_meeting(db, user_id, meeting_id, True)
        now = datetime.now(UTC)
        scoped_project = claim_project(db, user_id, project_id).id if project_id else None
        if meeting is None:
            db.add(MeetingModel(
                id=meeting_id, user_id=user_id, project_id=scoped_project, title=title,
                status="QUEUED", ingest_requested_at=now, source="link",
            ))
        elif meeting.ingest_requested_at is not None:
            db.rollback()
            return False
        else:
            meeting.ingest_requested_at = now
            meeting.ingest_claimed_at = None
            meeting.source = "link"
            meeting.purge_at = None
            if title:
                meeting.title = title
            if scoped_project:
                meeting.project_id = scoped_project
        db.commit()
        return True
    except IntegrityError:  # a concurrent request inserted the same new meeting first
        db.rollback()
        return False
    except Exception:
        db.rollback()
        raise


@router.post(
    "/ingest-link",
    response_model=IngestLinkResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(limit_by_user("rate_limit_ingest_link"))],
    summary="Asynchronous Meeting Link Ingestion",
    description=(
        "Accepts a meeting link (Fireflies dashboard view or raw audio/video recording), "
        "validates the link format, schedules an asynchronous background task to fetch "
        "or upload the media, forwards the structured transcript to FS-AI-Engine via HTTP, "
        "and persists the resulting 4-tier hierarchy for instant chatbot search."
    ),
)
async def ingest_meeting_link(
    payload: IngestLinkRequest,
    user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> IngestLinkResponse:
    url_str = str(payload.link).strip()

    # 1. Parse and validate link format
    try:
        parsed_link = parse_meeting_link(url_str)
    except ValueError as val_err:
        logger.warning(f"Rejected ingest-link request: {val_err} (URL: {url_str})")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(val_err),
        ) from val_err

    link_type = parsed_link["type"]
    identifier = parsed_link["id"]

    # 2. Queue the job for the in-process runner
    already_ingested = False
    if link_type == "FIREFLIES_DASHBOARD":
        effective_title = payload.title or parsed_link.get("title")
        existing = await run_in_threadpool(get_owned_meeting, db, user.id, identifier)
        already_ingested = existing is not None and existing.status == "PROCESSED"
        queued = await run_in_threadpool(_queue_meeting, db, user.id, identifier, payload.project_id, effective_title)
        msg = (
            f"Fireflies transcript '{identifier}' queued for background retrieval, "
            f"AI chunking, and database persistence."
        )
        if already_ingested:
            msg = (
                f"Meeting '{identifier}' is already in your library. It will only be re-processed "
                f"if its transcript has changed in Fireflies."
            )
        if queued:
            ingest_runner.wake()
    else:  # RAW_MEDIA
        queued = True
        ingest_runner.submit_media(identifier, payload.title)
        msg = (
            f"Raw media recording from '{identifier}' submitted to Fireflies API "
            f"for asynchronous transcription."
        )
    if not queued:
        msg = "This meeting is already being processed."
        already_ingested = False

    logger.info(f"Accepted link ingestion for {link_type} ({identifier}).")

    return IngestLinkResponse(
        status="queued",
        already_ingested=already_ingested,
        job_type=link_type,
        message=msg,
        meeting_identifier=identifier,
        project_id=payload.project_id,
        target_link=url_str,
    )
