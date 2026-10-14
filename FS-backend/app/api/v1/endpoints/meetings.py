
from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from ....api.deps import get_current_user
from ....core.config import Settings, get_settings
from ....core.database import UserModel, get_db
from ....schemas.models import (
    MeetingListResponse,
    MeetingResponse,
    ProjectListResponse,
    SpeakerTurnResponse,
)
from ....services.meeting_service import MeetingService

router = APIRouter()


def get_meeting_service(
    db: Session = Depends(get_db),
    user: UserModel = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> MeetingService:
    """Dependency provider for MeetingService, scoped to the logged-in account."""
    return MeetingService(db=db, user_id=user.id, settings=settings)


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
    response_model=list[SpeakerTurnResponse],
    status_code=status.HTTP_200_OK,
    summary="Get Level 1 speaker turns for a meeting",
    description="Returns the ordered list of normalized speaker turns (mtg_<id>_turn_<NNN>).",
)
def get_meeting_turns(
    meeting_id: str,
    service: MeetingService = Depends(get_meeting_service),
) -> list[SpeakerTurnResponse]:
    """Thin route handler returning Level 1 speaker turns for a meeting."""
    return service.get_meeting_turns(meeting_id)
