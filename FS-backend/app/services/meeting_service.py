import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import HTTPException, status
from sqlalchemy import and_, func, text
from sqlalchemy.orm import Session, defer

from ..core.config import Settings, get_settings
from ..core.database import MeetingModel, MeetingTopicModel, SpeakerTurnModel
from ..schemas.models import (
    MeetingResponse,
    MeetingSummary,
    SpeakerTurnResponse,
)
from . import failure_kinds
from .fireflies_client import ROOT_DIR
from .ownership import OwnershipError, claim_project, get_owned_meeting, unscope_project_id

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

def ingestion_status(meeting, live_claim: bool, retry_open: bool) -> str:
    """API-facing ingestion state (plan section 8). live_claim and retry_open are computed in SQL."""
    if meeting.status == "PROCESSED":
        return "processed"
    if live_claim:
        return "processing"
    if meeting.ingest_requested_at is not None or meeting.status != "FAILED":
        return "queued"
    if meeting.retry_until is None:
        return "skipped"
    return "retrying" if retry_open else "retries_stopped"


def failure_view(meeting, retry_open: bool) -> dict:
    """failure_kind, next_step and status_label for a FAILED meeting (all None otherwise). Rows recorded before
    failure_kind existed use the retry rule alone."""
    if meeting.status != "FAILED":
        return {"failure_kind": None, "next_step": None, "status_label": None}
    until = meeting.retry_until.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC") if meeting.retry_until else None
    key = meeting.failure_kind if failure_kinds.kind(meeting.failure_kind) else None
    next_step = failure_kinds.next_step(key, retry_open)
    if next_step is None:
        next_step = failure_kinds.AUTO_RETRY_STEP if retry_open else failure_kinds.RETRIES_STOPPED_STEP
    return {"failure_kind": key, "next_step": next_step, "status_label": failure_kinds.status_label(key, retry_open, until)}


class MeetingService:
    """Read-side service for one account's meetings, projects and speaker turns (ingestion lives in pipeline_orchestrator)."""

    def __init__(self, db: Session, user_id: str, settings: Settings | None = None):
        self.db = db
        self.user_id = user_id
        self.settings = settings or get_settings()

    def _summaries(self, meeting_id: str | None = None) -> list[MeetingSummary]:
        turn_counts = (
            self.db.query(SpeakerTurnModel.meeting_id, func.count().label("turn_count"))
            .filter(SpeakerTurnModel.user_id == self.user_id)
            .group_by(SpeakerTurnModel.meeting_id)
            .subquery()
        )
        live_claim = and_(
            MeetingModel.ingest_claimed_at.isnot(None),
            MeetingModel.ingest_claimed_at > func.now() - timedelta(minutes=30),
        )
        query = (
            self.db.query(
                MeetingModel,
                turn_counts.c.turn_count,
                live_claim.label("live_claim"),
                (MeetingModel.retry_until > func.now()).label("retry_open"),
            )
            .outerjoin(turn_counts, turn_counts.c.meeting_id == MeetingModel.id)
            .filter(MeetingModel.user_id == self.user_id, MeetingModel.excluded_at.is_(None))
        )
        if meeting_id is not None:
            query = query.filter(MeetingModel.id == meeting_id)
        rows = query.order_by(MeetingModel.created_at.desc()).all()
        return [
            MeetingSummary(
                id=m.id,
                title=m.title or f"Meeting {m.id}",
                summary=m.summary,
                date=normalize_meeting_date(m.date),
                duration=m.duration,
                status=m.status,
                ingestion_status=ingestion_status(m, bool(live), bool(retry_open)),
                source=m.source,
                purge_at=m.purge_at,
                retry_until=m.retry_until,
                **failure_view(m, bool(retry_open)),
                project_id=unscope_project_id(m.project_id, self.user_id),
                total_turns=turn_count or 0,
                created_at=m.created_at,
                updated_at=m.updated_at,
            )
            for m, turn_count, live, retry_open in rows
        ]

    def list_meetings(self) -> list[MeetingSummary]:
        return self._summaries()

    def get_summary(self, meeting_id: str) -> MeetingSummary:
        rows = self._summaries(meeting_id)
        if not rows:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Meeting '{meeting_id}' not found.")
        return rows[0]

    def set_project(self, meeting_id: str, project_name: str | None) -> MeetingSummary:
        """Moves the meeting (row and its turns' metadata) to a project, or out of all, in one transaction."""
        try:
            meeting = get_owned_meeting(self.db, self.user_id, meeting_id, for_update=True)
        except OwnershipError:
            meeting = None
        if meeting is not None and meeting.excluded_at is not None:
            meeting = None
        if meeting is None:
            self.db.rollback()
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Meeting '{meeting_id}' not found.")
        name = (project_name or "").strip()
        project = claim_project(self.db, self.user_id, name) if name else None
        meeting.project_id = project.id if project else None
        self.db.execute(
            text(
                "UPDATE speaker_turns SET metadata = jsonb_set(coalesce(metadata, '{}'::jsonb), "
                "'{project_id}', to_jsonb(CAST(:value AS text))) WHERE meeting_id = :m AND user_id = :u"
            ),
            {"value": project.id if project else "default-project", "m": meeting_id, "u": self.user_id},
        )
        self.db.commit()
        return self.get_summary(meeting_id)

    def request_retry(self, meeting_id: str, has_fireflies_key: bool) -> None:
        """Queues a re-check for the runner. Raises 404/409 per plan section 8."""
        try:
            meeting = get_owned_meeting(self.db, self.user_id, meeting_id, for_update=True)
        except OwnershipError:
            meeting = None
        if meeting is not None and meeting.excluded_at is not None:
            meeting = None
        error = None
        if meeting is None:
            self.db.rollback()
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Meeting '{meeting_id}' not found.")
        if meeting.status == "PROCESSED":
            error = "This meeting is already processed."
        elif meeting.ingest_requested_at is not None:
            error = "This meeting is already queued."
        elif meeting.source == "fireflies_sync" and not has_fireflies_key:
            error = "Reconnect Fireflies to retry this meeting."
        elif self.db.execute(
            text("SELECT updated_at > now() - interval '60 seconds' FROM meetings WHERE id = :m"),
            {"m": meeting_id},
        ).scalar():
            error = "A retry was just requested. Wait a minute before retrying again."
        if error:
            self.db.rollback()
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=error)
        meeting.ingest_requested_at = func.now()
        meeting.ingest_claimed_at = None
        meeting.updated_at = func.now()
        self.db.commit()

    def ensure_deletable_target(self, meeting_id: str) -> None:
        """404 unless the account owns a meeting that is not excluded."""
        meeting = (
            self.db.query(MeetingModel)
            .filter(MeetingModel.id == meeting_id, MeetingModel.user_id == self.user_id)
            .first()
        )
        if meeting is None or meeting.excluded_at is not None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Meeting '{meeting_id}' not found.")

    def delete_meeting(self, meeting_id: str) -> bool:
        """Deletes a meeting (link) or turns it into an EXCLUDED marker (synced). Returns kept_marker."""
        try:
            meeting = get_owned_meeting(self.db, self.user_id, meeting_id, for_update=True)
        except OwnershipError:
            meeting = None
        if meeting is None or meeting.excluded_at is not None:
            self.db.rollback()
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Meeting '{meeting_id}' not found.")
        if meeting.ingest_requested_at is not None:
            self.db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This meeting is being processed. Try again when it has finished.",
            )
        keep = meeting.source == "fireflies_sync"
        if keep:
            for model in (MeetingTopicModel, SpeakerTurnModel):
                self.db.query(model).filter(model.meeting_id == meeting_id, model.user_id == self.user_id).delete(
                    synchronize_session=False
                )
            meeting.status = "EXCLUDED"
            meeting.excluded_at = func.now()
            meeting.summary = None
            meeting.content_hash = None
            meeting.retry_until = None
        else:
            self.db.delete(meeting)
        self.db.commit()
        if not keep:
            self._remove_raw_transcript(meeting_id)
        logger.info("Deleted meeting %s (kept_marker=%s)", meeting_id, keep)
        return keep

    def _remove_raw_transcript(self, meeting_id: str) -> None:
        try:
            def _safe(value: str) -> str:
                return "".join(c if c.isalnum() or c in ("-", "_", ".") else "_" for c in str(value))

            base = Path(self.settings.raw_transcripts_dir)
            if not base.is_absolute():
                base = ROOT_DIR / base
            (base / _safe(self.user_id) / f"{_safe(meeting_id)}.json").unlink(missing_ok=True)
        except Exception:
            logger.warning("Could not remove raw transcript for meeting %s", meeting_id)

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
            .filter(
                MeetingModel.id == meeting_id,
                MeetingModel.user_id == self.user_id,
                MeetingModel.excluded_at.is_(None),
            )
            .first()
        )
        if not meeting:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Meeting '{meeting_id}' not found.",
            )

        summary = self.get_summary(meeting_id)
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
            ingestion_status=summary.ingestion_status,
            source=meeting.source,
            purge_at=meeting.purge_at,
            retry_until=meeting.retry_until,
            failure_kind=summary.failure_kind,
            next_step=summary.next_step,
            status_label=summary.status_label,
            project_id=unscope_project_id(meeting.project_id, self.user_id),
            total_turns=len(turn_responses),
            created_at=meeting.created_at,
            updated_at=meeting.updated_at,
            turns=turn_responses,
        )

    def get_meeting_turns(self, meeting_id: str) -> list[SpeakerTurnResponse]:
        meeting = self.get_meeting_by_id(meeting_id)
        return meeting.turns or []
