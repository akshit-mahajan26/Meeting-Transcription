import logging
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session, defer

from ..core.config import Settings, get_settings
from ..core.database import MeetingModel, SpeakerTurnModel
from ..schemas.models import (
    MeetingResponse,
    MeetingSummary,
    SpeakerTurnResponse,
)
from .ownership import unscope_project_id

logger = logging.getLogger(__name__)


def normalize_meeting_date(raw: str | None) -> str | None:
    """Meeting start as an ISO-8601 UTC string.

    Fireflies reports the start as epoch milliseconds, which is what older rows store.
    Values that are already ISO dates, or anything unrecognised, pass through unchanged.
    """
    if not raw:
        return None
    text = str(raw).strip()
    try:
        number = float(text)
    except ValueError:
        return text
    if number <= 0:
        return None
    seconds = number / 1000 if number > 1e11 else number  # epoch ms vs seconds
    return datetime.fromtimestamp(seconds, tz=UTC).isoformat()

def ingestion_status(meeting) -> str:
    """API-facing ingestion state: processing, queued, processed or failed."""
    if meeting.ingest_claimed_at is not None:
        return "processing"
    if meeting.ingest_requested_at is not None:
        return "queued"
    if meeting.status == "PROCESSED":
        return "processed"
    if meeting.status == "FAILED":
        return "failed"
    return "queued"


class MeetingService:
    """Read-side service for one account's meetings, projects and speaker turns (ingestion lives in pipeline_orchestrator)."""

    def __init__(self, db: Session, user_id: str, settings: Settings | None = None):
        self.db = db
        self.user_id = user_id
        self.settings = settings or get_settings()

    def list_meetings(self) -> list[MeetingSummary]:
        turn_counts = (
            self.db.query(SpeakerTurnModel.meeting_id, func.count().label("turn_count"))
            .filter(SpeakerTurnModel.user_id == self.user_id)
            .group_by(SpeakerTurnModel.meeting_id)
            .subquery()
        )
        rows = (
            self.db.query(MeetingModel, turn_counts.c.turn_count)
            .outerjoin(turn_counts, turn_counts.c.meeting_id == MeetingModel.id)
            .filter(MeetingModel.user_id == self.user_id)
            .order_by(MeetingModel.created_at.desc())
            .all()
        )
        summaries: list[MeetingSummary] = []
        for m, turn_count in rows:
            summaries.append(
                MeetingSummary(
                    id=m.id,
                    title=m.title or f"Meeting {m.id}",
                    summary=m.summary,
                    date=normalize_meeting_date(m.date),
                    duration=m.duration,
                    status=m.status,
                    ingestion_status=ingestion_status(m),
                    project_id=unscope_project_id(m.project_id, self.user_id),
                    total_turns=turn_count or 0,
                    created_at=m.created_at,
                    updated_at=m.updated_at,
                )
            )
        return summaries

    def list_projects(self) -> list[str]:
        """Fetches distinct project identifiers across projects and meetings tables."""
        from ..core.database import ProjectModel
        proj_ids = {
            unscope_project_id(p.id, self.user_id)
            for p in self.db.query(ProjectModel).filter(ProjectModel.user_id == self.user_id).all()
            if p.id
        }
        mtg_proj_ids = {
            unscope_project_id(m.project_id, self.user_id)
            for m in self.db.query(MeetingModel)
            .filter(MeetingModel.user_id == self.user_id, MeetingModel.project_id.isnot(None))
            .all()
            if m.project_id
        }
        all_ids = proj_ids.union(mtg_proj_ids)
        return sorted([pid for pid in all_ids if pid and pid.strip()])

    def get_meeting_by_id(self, meeting_id: str) -> MeetingResponse:
        meeting = (
            self.db.query(MeetingModel)
            .filter(MeetingModel.id == meeting_id, MeetingModel.user_id == self.user_id)
            .first()
        )
        if not meeting:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Meeting '{meeting_id}' not found.",
            )

        turns = (
            self.db.query(SpeakerTurnModel).options(defer(SpeakerTurnModel.embedding))
            .filter(SpeakerTurnModel.meeting_id == meeting_id, SpeakerTurnModel.user_id == self.user_id)
            .order_by(SpeakerTurnModel.turn_number.asc())
            .all()
        )

        turn_responses = [
            SpeakerTurnResponse(
                turn_id=t.turn_id,
                meeting_id=t.meeting_id,
                turn_number=t.turn_number,
                speaker_id=t.speaker_id,
                speaker_name=t.speaker_name,
                text=t.text,
                start_time=t.start_time,
                end_time=t.end_time,
                duration=t.duration,
                sentence_indices=t.sentence_indices,
                is_question=bool(t.is_question),
                is_commitment=bool(t.is_commitment),
                is_blocker=bool(t.is_blocker),
            )
            for t in turns
        ]

        return MeetingResponse(
            id=meeting.id,
            title=meeting.title or f"Meeting {meeting.id}",
            summary=meeting.summary,
            date=normalize_meeting_date(meeting.date),
            duration=meeting.duration,
            status=meeting.status,
            ingestion_status=ingestion_status(meeting),
            project_id=unscope_project_id(meeting.project_id, self.user_id),
            total_turns=len(turn_responses),
            created_at=meeting.created_at,
            updated_at=meeting.updated_at,
            turns=turn_responses,
        )

    def get_meeting_turns(self, meeting_id: str) -> list[SpeakerTurnResponse]:
        meeting = self.get_meeting_by_id(meeting_id)
        return meeting.turns or []
