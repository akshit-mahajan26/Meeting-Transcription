
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, SecretStr
from sqlalchemy.orm import Session

from ....api.deps import get_current_user
from ....core.config import Settings, get_settings
from ....core.database import UserModel, get_db
from ....core.ratelimit import limit_by_user
from ....core.security import verify_password
from ....schemas.models import (
    MeetingListResponse,
    MeetingResponse,
    MeetingSummary,
    ProjectListResponse,
    SpeakerTurnResponse,
)
from ....services.ingest_runner import ingest_runner
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


class ProjectUpdate(BaseModel):
    project_id: str | None = None


@router.patch(
    "/{meeting_id}",
    response_model=MeetingSummary,
    status_code=status.HTTP_200_OK,
    summary="Move a meeting to a project or remove it from its project",
)
async def update_meeting_project(
    meeting_id: str,
    body: ProjectUpdate,
    service: MeetingService = Depends(get_meeting_service),
) -> MeetingSummary:
    return await run_in_threadpool(service.set_project, meeting_id, body.project_id)


@router.post(
    "/{meeting_id}/retry",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(limit_by_user("rate_limit_retry"))],
    summary="Queue a failed meeting for another ingestion attempt",
)
async def retry_meeting(
    meeting_id: str,
    service: MeetingService = Depends(get_meeting_service),
    user: UserModel = Depends(get_current_user),
) -> dict[str, str]:
    await run_in_threadpool(service.request_retry, meeting_id, bool(user.fireflies_api_key_encrypted))
    ingest_runner.wake()
    return {"status": "queued"}


class DeleteRequest(BaseModel):
    password: SecretStr


@router.post(
    "/{meeting_id}/delete",
    status_code=status.HTTP_200_OK,
    summary="Delete a meeting after confirming the account password",
)
async def delete_meeting(
    meeting_id: str,
    body: DeleteRequest,
    service: MeetingService = Depends(get_meeting_service),
    user: UserModel = Depends(get_current_user),
) -> dict[str, bool]:
    # Ownership/exclusion 404 comes first so a wrong password cannot probe other accounts' ids.
    await run_in_threadpool(service.ensure_deletable_target, meeting_id)
    # 403, not 401: the frontend logs the user out on any 401.
    if not await run_in_threadpool(verify_password, body.password.get_secret_value(), user.password_hash):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Incorrect password.")
    kept = await run_in_threadpool(service.delete_meeting, meeting_id)
    return {"deleted": True, "kept_marker": kept}
