import re
import logging
from urllib.parse import urlparse
from typing import Optional, Dict, Any
from fastapi import APIRouter, BackgroundTasks, HTTPException, status, Depends
from pydantic import BaseModel, HttpUrl, Field

from ...services.fireflies_client import FirefliesClient, FirefliesAPIError, get_fireflies_client
from ...services.pipeline_orchestrator import BackendPipelineOrchestrator, get_backend_pipeline_orchestrator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/meetings", tags=["meetings"])

SUPPORTED_MEDIA_EXTENSIONS = (".mp3", ".mp4", ".wav", ".m4a", ".aac", ".ogg", ".webm", ".flac")


class IngestLinkRequest(BaseModel):
    """Request body for asynchronous meeting link ingestion."""
    link: HttpUrl = Field(..., description="Fireflies dashboard view link or public audio/video file link")
    project_id: Optional[str] = Field(default=None, description="Parent project identifier")
    title: Optional[str] = Field(default=None, description="Optional custom title for the meeting")


class IngestLinkResponse(BaseModel):
    """Immediate HTTP 202 Accepted response payload."""
    status: str = "queued"
    job_type: str
    message: str
    meeting_identifier: str
    project_id: Optional[str] = None
    target_link: str


def parse_meeting_link(url_str: str) -> Dict[str, Any]:
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
    host = (parsed.netloc or "").lower()
    path = parsed.path or ""

    # 1. Fireflies Dashboard Link Check
    if "fireflies.ai" in host:
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


async def _process_fireflies_dashboard_task(
    transcript_id: str,
    project_id: Optional[str],
    title: Optional[str],
    fireflies_svc: FirefliesClient,
    orchestrator_svc: BackendPipelineOrchestrator,
) -> None:
    """Background worker task to fetch transcript from Fireflies and orchestrate downstream ingestion."""
    logger.info(f"[Background Task] Initiating transcript fetch for ID: '{transcript_id}'...")
    try:
        transcript_data = await fireflies_svc.fetch_transcript_by_id(transcript_id=transcript_id)
        logger.info(f"[Background Task] Fetched transcript for '{transcript_id}'. Running AI Engine pipeline...")
        result = await orchestrator_svc.orchestrate_meeting_ingestion(
            transcript_dict=transcript_data,
            project_id=project_id,
            custom_title=title,
        )
        logger.info(f"[Background Task] Successfully ingested meeting '{transcript_id}': {result}")
    except Exception as exc:
        logger.error(f"[Background Task] Failed to process transcript '{transcript_id}': {exc}", exc_info=True)


async def _process_raw_media_task(
    media_url: str,
    title: Optional[str],
    fireflies_svc: FirefliesClient,
) -> None:
    """Background worker task to submit external media recording to Fireflies uploadAudio mutation."""
    logger.info(f"[Background Task] Submitting media file URL to Fireflies: '{media_url}'...")
    try:
        result = await fireflies_svc.upload_audio_from_url(audio_url=media_url, title=title)
        logger.info(f"[Background Task] Audio upload mutation completed successfully: {result}")
    except Exception as exc:
        logger.error(f"[Background Task] Failed to upload audio file '{media_url}': {exc}", exc_info=True)


@router.post(
    "/ingest-link",
    response_model=IngestLinkResponse,
    status_code=status.HTTP_202_ACCEPTED,
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
    background_tasks: BackgroundTasks,
    fireflies_svc: FirefliesClient = Depends(get_fireflies_client),
    orchestrator_svc: BackendPipelineOrchestrator = Depends(get_backend_pipeline_orchestrator),
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
        )

    link_type = parsed_link["type"]
    identifier = parsed_link["id"]

    # 2. Queue appropriate background worker
    if link_type == "FIREFLIES_DASHBOARD":
        effective_title = payload.title or parsed_link.get("title")
        background_tasks.add_task(
            _process_fireflies_dashboard_task,
            transcript_id=identifier,
            project_id=payload.project_id,
            title=effective_title,
            fireflies_svc=fireflies_svc,
            orchestrator_svc=orchestrator_svc,
        )
        msg = (
            f"Fireflies transcript '{identifier}' queued for background retrieval, "
            f"AI chunking, and database persistence."
        )
    else:  # RAW_MEDIA
        background_tasks.add_task(
            _process_raw_media_task,
            media_url=identifier,
            title=payload.title,
            fireflies_svc=fireflies_svc,
        )
        msg = (
            f"Raw media recording from '{identifier}' submitted to Fireflies API "
            f"for asynchronous transcription."
        )

    logger.info(f"Queued background link ingestion for {link_type} ({identifier}).")

    return IngestLinkResponse(
        status="queued",
        job_type=link_type,
        message=msg,
        meeting_identifier=identifier,
        project_id=payload.project_id,
        target_link=url_str,
    )
