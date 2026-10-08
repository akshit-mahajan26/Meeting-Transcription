import json
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from ....core.database import get_db
from ....core.config import Settings, get_settings
from ....core.security import verify_hmac_sha256_signature
from ....schemas.models import (
    MeetingResponse,
    MeetingListResponse,
    ProjectListResponse,
    SpeakerTurnResponse,
    RawTranscriptIngestRequest,
    FetchMeetingIngestRequest,
    WebhookResponse,
    FirefliesWebhookPayload,
)
from ....services.meeting_service import MeetingService

router = APIRouter()


def get_meeting_service(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> MeetingService:
    """Dependency provider for MeetingService."""
    return MeetingService(db=db, settings=settings)


@router.post(
    "/webhook",
    response_model=WebhookResponse,
    status_code=status.HTTP_200_OK,
    summary="Fireflies.ai Webhook Handler (HMAC Protected)",
    description="Receives transcription completion notifications from Fireflies. Protected by HMAC-SHA256 signature verification.",
)
async def handle_fireflies_webhook(
    raw_body: bytes = Depends(verify_hmac_sha256_signature),
    service: MeetingService = Depends(get_meeting_service),
) -> WebhookResponse:
    """
    HMAC-verified webhook handler.
    1. HMAC-SHA256 signature is verified by the dependency.
    2. Body is decoded into JSON payload.
    3. Delegated to MeetingService for processing.
    """
    try:
        payload_data = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Malformed JSON in webhook request body: {str(exc)}",
        )

    return service.handle_webhook_event(payload_data)


@router.post(
    "/webhook/fireflies",
    response_model=WebhookResponse,
    status_code=status.HTTP_200_OK,
    summary="Fireflies Webhook & Manual Swagger Ingestion",
    description="Allows manual upload or Swagger invocation of Fireflies webhook events without signature rejection.",
)
async def handle_fireflies_webhook_manual(
    payload: FirefliesWebhookPayload,
    service: MeetingService = Depends(get_meeting_service),
) -> WebhookResponse:
    """Manual/Swagger trigger for Fireflies webhook payloads."""
    return service.handle_webhook_event(payload.model_dump())


@router.post(
    "/ingest/raw",
    response_model=MeetingResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Direct raw transcript ingestion",
    description="Send raw transcript sentences directly to normalize into Level 1 turns and persist.",
)
def ingest_raw_transcript(
    request: RawTranscriptIngestRequest,
    service: MeetingService = Depends(get_meeting_service),
) -> MeetingResponse:
    """Thin route handler delegating to MeetingService.ingest_raw_transcript."""
    return service.ingest_raw_transcript(request)


@router.post(
    "/ingest/fetch",
    response_model=MeetingResponse,
    status_code=status.HTTP_200_OK,
    summary="Fetch and ingest transcript from Fireflies",
    description="Instructs the AI Engine to query Fireflies GraphQL API by meeting ID and normalize the result.",
)
def ingest_by_fetching(
    request: FetchMeetingIngestRequest,
    service: MeetingService = Depends(get_meeting_service),
) -> MeetingResponse:
    """Thin route handler delegating to MeetingService.ingest_from_fireflies_api."""
    return service.ingest_from_fireflies_api(request)


@router.get(
    "/",
    response_model=MeetingListResponse,
    status_code=status.HTTP_200_OK,
    summary="List ingested meetings",
    description="Returns a list of all normalized meetings stored in the system.",
)
def list_meetings(
    service: MeetingService = Depends(get_meeting_service),
) -> MeetingListResponse:
    """Thin route handler returning summaries of all meetings."""
    summaries = service.list_meetings()
    return MeetingListResponse(total=len(summaries), meetings=summaries)


@router.get(
    "/projects",
    response_model=ProjectListResponse,
    status_code=status.HTTP_200_OK,
    summary="List all distinct projects",
    description="Returns a list of all existing project IDs for organization and ingestion selection.",
)
def list_projects(
    service: MeetingService = Depends(get_meeting_service),
) -> ProjectListResponse:
    """Returns all known project identifiers across meetings and projects."""
    projects = service.list_projects()
    return ProjectListResponse(total=len(projects), projects=projects)


@router.get(
    "/{meeting_id}",
    response_model=MeetingResponse,
    status_code=status.HTTP_200_OK,
    summary="Get meeting details and Level 1 turns",
    description="Fetches meeting metadata and all normalized speaker turns for a given meeting ID.",
)
def get_meeting(
    meeting_id: str,
    service: MeetingService = Depends(get_meeting_service),
) -> MeetingResponse:
    """Thin route handler returning meeting by ID."""
    return service.get_meeting_by_id(meeting_id)


@router.get(
    "/{meeting_id}/turns",
    response_model=List[SpeakerTurnResponse],
    status_code=status.HTTP_200_OK,
    summary="Get Level 1 speaker turns for a meeting",
    description="Returns the ordered list of normalized speaker turns (mtg_<id>_turn_<NNN>).",
)
def get_meeting_turns(
    meeting_id: str,
    service: MeetingService = Depends(get_meeting_service),
) -> List[SpeakerTurnResponse]:
    """Thin route handler returning Level 1 speaker turns for a meeting."""
    return service.get_meeting_turns(meeting_id)
