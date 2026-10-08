import json
import logging
from typing import Optional, List, Dict, Any
import httpx
from fastapi import HTTPException, status
from sqlalchemy.orm import Session
from ..core.config import Settings, get_settings
from ..core.database import MeetingModel, SpeakerTurnModel
from ..schemas.models import (
    MeetingResponse,
    MeetingSummary,
    SpeakerTurnResponse,
    RawTranscriptIngestRequest,
    FetchMeetingIngestRequest,
    WebhookResponse,
)

logger = logging.getLogger(__name__)


class MeetingService:
    """Service layer coordinating meeting transcription ingestion and persistence."""

    def __init__(self, db: Session, settings: Optional[Settings] = None):
        self.db = db
        self.settings = settings or get_settings()
        self.ai_engine_url = self.settings.ai_engine_url.rstrip("/")

    def handle_webhook_event(self, payload_dict: Dict[str, Any]) -> WebhookResponse:
        """
        Handle Fireflies webhook event, trigger transcript processing via AI-Engine,
        and store normalized results in the database.
        Supports both live GraphQL fetch events and manual payloads with sentences.
        """
        event_type = str(
            payload_dict.get("eventType")
            or payload_dict.get("event")
            or payload_dict.get("type")
            or ""
        ).lower()

        meeting_id = (
            payload_dict.get("meetingId")
            or payload_dict.get("meeting_id")
            or (payload_dict.get("data", {}).get("meetingId") if isinstance(payload_dict.get("data"), dict) else None)
            or (payload_dict.get("data", {}).get("id") if isinstance(payload_dict.get("data"), dict) else None)
        )

        # 1. Handle Fireflies Test Events / Verification Pings without sentences
        is_test = (
            "test" in event_type
            or "ping" in event_type
            or not meeting_id
        )

        if is_test and not meeting_id and not payload_dict.get("sentences"):
            logger.info("Acknowledged Fireflies test/ping webhook without meetingId.")
            return WebhookResponse(
                status="success",
                message="Fireflies test webhook verified successfully.",
                meeting_id="test",
                total_turns=0,
            )

        meeting_id_str = str(meeting_id or "mtg_manual_001").strip()

        # 2. If payload already includes pre-parsed sentences (e.g. manual Swagger trigger or webhook with transcript)
        if payload_dict.get("sentences"):
            logger.info(f"Ingesting pre-parsed sentences directly for meeting '{meeting_id_str}'.")
            try:
                raw_req = RawTranscriptIngestRequest(
                    meeting_id=meeting_id_str,
                    title=payload_dict.get("title") or f"Meeting {meeting_id_str}",
                    date=payload_dict.get("date"),
                    duration=payload_dict.get("duration"),
                    sentences=payload_dict["sentences"],
                )
                meeting = self.ingest_raw_transcript(raw_req)
                return WebhookResponse(
                    status="success",
                    message=f"Meeting '{meeting_id_str}' successfully processed and stored.",
                    meeting_id=meeting_id_str,
                    total_turns=meeting.total_turns,
                )
            except Exception as e:
                logger.error(f"Error processing pre-parsed sentences for '{meeting_id_str}': {e}")
                raise

        # 3. If explicit test event with dummy ID and no sentences, acknowledge immediately
        if is_test or meeting_id_str.lower() in ["test", "sample", "test12345", "mock", "dummy"]:
            logger.info(f"Acknowledged Fireflies test event for meeting '{meeting_id_str}'.")
            return WebhookResponse(
                status="success",
                message=f"Test event for meeting '{meeting_id_str}' acknowledged.",
                meeting_id=meeting_id_str,
                total_turns=0,
            )

        # 4. Real transcription completed event: fetch full transcript via GraphQL & process
        try:
            fetch_req = FetchMeetingIngestRequest(meeting_id=meeting_id_str)
            meeting = self.ingest_from_fireflies_api(fetch_req)
            return WebhookResponse(
                status="success",
                message=f"Meeting '{meeting_id_str}' successfully processed and stored.",
                meeting_id=meeting_id_str,
                total_turns=meeting.total_turns,
            )
        except HTTPException as exc:
            # If Fireflies GraphQL returns 404 for a synthetic test event, acknowledge gracefully
            if exc.status_code in [404, 502, 500]:
                logger.warning(
                    f"Fireflies transcript '{meeting_id_str}' could not be fetched ({exc.detail}). "
                    f"Acknowledging event as test/mock verification."
                )
                return WebhookResponse(
                    status="success",
                    message=f"Webhook event received for '{meeting_id_str}'.",
                    meeting_id=meeting_id_str,
                    total_turns=0,
                )
            raise

    def ingest_raw_transcript(self, request: RawTranscriptIngestRequest) -> MeetingResponse:
        """Send raw sentences to FS-AI-Engine for normalization, then store in DB."""
        target_url = f"{self.ai_engine_url}/api/v1/transcript/normalize"

        payload = {
            "meeting_id": request.meeting_id,
            "title": request.title,
            "date": request.date,
            "duration": request.duration,
            "sentences": [s.model_dump() for s in request.sentences],
        }

        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(target_url, json=payload)
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Unable to connect to FS-AI-Engine at {self.ai_engine_url}: {str(exc)}",
            ) from exc

        if resp.status_code != 200:
            raise HTTPException(
                status_code=resp.status_code,
                detail=f"FS-AI-Engine error: {resp.text}",
            )

        norm_data = resp.json()
        return self._save_normalized_meeting(norm_data)

    def ingest_from_fireflies_api(self, request: FetchMeetingIngestRequest) -> MeetingResponse:
        """Request FS-AI-Engine to query Fireflies GraphQL API and normalize transcript."""
        target_url = f"{self.ai_engine_url}/api/v1/transcript/fetch-and-normalize"

        payload = {
            "meeting_id": request.meeting_id,
            "api_key": request.api_key,
        }

        try:
            with httpx.Client(timeout=45.0) as client:
                resp = client.post(target_url, json=payload)
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Unable to connect to FS-AI-Engine at {self.ai_engine_url}: {str(exc)}",
            ) from exc

        if resp.status_code != 200:
            raise HTTPException(
                status_code=resp.status_code,
                detail=f"FS-AI-Engine processing failure: {resp.text}",
            )

        norm_data = resp.json()
        return self._save_normalized_meeting(norm_data)

    def _save_normalized_meeting(self, norm_data: Dict[str, Any]) -> MeetingResponse:
        """Persist or update meeting and speaker turns in the database."""
        meeting_id = str(norm_data["meeting_id"])
        title = norm_data.get("title")
        date_str = norm_data.get("date")
        duration = norm_data.get("duration")
        summary = (
            norm_data.get("summary")
            or norm_data.get("executive_summary")
            or (norm_data.get("level3_summary") or {}).get("overview")
            or ""
        )

        meeting = self.db.query(MeetingModel).filter(MeetingModel.id == meeting_id).first()
        if not meeting:
            meeting = MeetingModel(
                id=meeting_id,
                title=title,
                summary=summary,
                date=date_str,
                duration=duration,
                status="PROCESSED",
            )
            self.db.add(meeting)
            self.db.flush()
        else:
            meeting.title = title or meeting.title
            if summary:
                meeting.summary = summary
            meeting.date = date_str or meeting.date
            meeting.duration = duration or meeting.duration
            meeting.status = "PROCESSED"
            self.db.query(SpeakerTurnModel).filter(SpeakerTurnModel.meeting_id == meeting_id).delete()
            self.db.flush()

        turns_payload = norm_data.get("turns", [])

        def _fmt_ts(sec: float) -> str:
            s = max(0, int(round(float(sec or 0.0))))
            return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"

        # Check for missing embeddings and batch fetch via AI Engine
        missing_indices = []
        texts_to_embed = []
        for idx, t in enumerate(turns_payload):
            emb = t.get("embedding")
            if not (emb and len(emb) == 1536):
                missing_indices.append(idx)
                spk = str(t.get("speaker_name") or t.get("speaker") or "Unknown Speaker")
                txt = str(t.get("text") or "").strip()
                ts = _fmt_ts(float(t.get("start_time", 0.0)))
                texts_to_embed.append(f"[{ts}] {spk}: {txt}")

        if missing_indices and texts_to_embed:
            try:
                with httpx.Client(timeout=60.0) as client:
                    emb_resp = client.post(
                        f"{self.ai_engine_url}/api/v1/transcript/embed",
                        json={"texts": texts_to_embed},
                    )
                    if emb_resp.status_code == 200:
                        emb_list = emb_resp.json().get("embeddings", [])
                        for m_idx, vec in zip(missing_indices, emb_list):
                            if len(vec) == 1536:
                                turns_payload[m_idx]["embedding"] = vec
            except Exception as emb_err:
                logger.warning(f"Could not batch fetch missing embeddings: {emb_err}")

        turn_models = []
        for t in turns_payload:
            spk_name = str(t.get("speaker_name") or t.get("speaker") or "Unknown Speaker")
            s_start = float(t["start_time"])
            s_end = float(t["end_time"])
            dur = float(t["duration"])
            is_q = bool(t.get("is_question", False))
            is_c = bool(t.get("is_commitment", False))
            is_b = bool(t.get("is_blocker", False))

            meta_dict = {
                "speaker": spk_name,
                "timestamp": _fmt_ts(s_start),
                "meeting_id": str(meeting_id),
                "project_id": "default-project",
                "signals": t.get("signals") or [],
                "is_question": is_q,
                "is_commitment": is_c,
                "is_blocker": is_b,
            }

            emb_vector = t.get("embedding")
            if not (emb_vector and len(emb_vector) == 1536):
                emb_vector = None

            turn_model = SpeakerTurnModel(
                turn_id=t["turn_id"],
                meeting_id=meeting_id,
                turn_number=int(t["turn_number"]),
                speaker_id=t["speaker_id"],
                speaker_name=spk_name,
                text=t["text"],
                start_time=s_start,
                end_time=s_end,
                duration=dur,
                is_question=is_q,
                is_commitment=is_c,
                is_blocker=is_b,
                _sentence_indices=json.dumps(t.get("sentence_indices", [])),
                embedding=emb_vector,
                meta_data=meta_dict,
            )
            turn_models.append(turn_model)

        self.db.bulk_save_objects(turn_models)
        self.db.commit()
        self.db.refresh(meeting)

        return self.get_meeting_by_id(meeting_id)

    def list_meetings(self) -> List[MeetingSummary]:
        meetings = self.db.query(MeetingModel).order_by(MeetingModel.created_at.desc()).all()
        summaries: List[MeetingSummary] = []
        for m in meetings:
            turn_count = self.db.query(SpeakerTurnModel).filter(SpeakerTurnModel.meeting_id == m.id).count()
            summaries.append(
                MeetingSummary(
                    id=m.id,
                    title=m.title or f"Meeting {m.id}",
                    summary=m.summary,
                    date=m.date,
                    duration=m.duration,
                    status=m.status,
                    project_id=m.project_id,
                    total_turns=turn_count,
                    created_at=m.created_at,
                )
            )
        return summaries

    def list_projects(self) -> List[str]:
        """Fetches distinct project identifiers across projects and meetings tables."""
        from ..core.database import ProjectModel
        proj_ids = {p.id for p in self.db.query(ProjectModel).all() if p.id}
        mtg_proj_ids = {
            m.project_id for m in self.db.query(MeetingModel).filter(MeetingModel.project_id.isnot(None)).all()
            if m.project_id
        }
        all_ids = proj_ids.union(mtg_proj_ids)
        return sorted([pid for pid in all_ids if pid and pid.strip()])

    def get_meeting_by_id(self, meeting_id: str) -> MeetingResponse:
        meeting = self.db.query(MeetingModel).filter(MeetingModel.id == meeting_id).first()
        if not meeting:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Meeting '{meeting_id}' not found.",
            )

        turns = (
            self.db.query(SpeakerTurnModel)
            .filter(SpeakerTurnModel.meeting_id == meeting_id)
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
            date=meeting.date,
            duration=meeting.duration,
            status=meeting.status,
            project_id=meeting.project_id,
            total_turns=len(turn_responses),
            created_at=meeting.created_at,
            updated_at=meeting.updated_at,
            turns=turn_responses,
        )

    def get_meeting_turns(self, meeting_id: str) -> List[SpeakerTurnResponse]:
        meeting = self.get_meeting_by_id(meeting_id)
        return meeting.turns or []
