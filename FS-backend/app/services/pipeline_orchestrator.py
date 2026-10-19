import asyncio
import hashlib
import json
import logging
import uuid
from typing import Any

import httpx
from sqlalchemy.orm import Session

from ..core.config import Settings, get_settings
from ..core.database import (
    MeetingModel,
    MeetingTopicModel,
    SessionLocal,
    SpeakerTurnModel,
)
from .ingestion_failures import PipelineOrchestratorError
from .ownership import claim_project, get_owned_meeting

logger = logging.getLogger(__name__)

def _fmt_ts(sec: float) -> str:
    s = max(0, round(float(sec or 0.0)))
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


class BackendPipelineOrchestrator:
    """
    Background Pipeline Orchestrator for FS-backend.
    Coordinates:
    1. Idempotency checksums on incoming transcripts.
    2. Zero-import HTTP communication with FS-AI-Engine for token chunking, boundary detection, and embeddings.
    3. Atomic transaction persistence across the 4-tier database schema:
       projects -> meetings -> meeting_topics -> speaker_turns.
    """

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        ai_base = (self.settings.ai_engine_url or "").rstrip("/")
        ai_base = ai_base.removesuffix("/api/v1")
        self.ai_engine_url = ai_base
        self.engine_headers = {"X-Internal-Api-Key": self.settings.engine_api_key}

    @staticmethod
    def compute_transcript_hash(sentences: list[dict[str, Any]]) -> str:
        """Compute SHA-256 hash of transcript text content for idempotency detection."""
        hasher = hashlib.sha256()
        for s in sentences:
            spk = str(s.get("speaker_name", ""))
            txt = str(s.get("text", "")).strip()
            line = f"{spk}:{txt}\n"
            hasher.update(line.encode("utf-8"))
        return hasher.hexdigest()

    async def _dispatch_to_ai_engine(
        self,
        payload: dict[str, Any],
        max_retries: int = 3,
        initial_backoff: float = 1.0,
    ) -> dict[str, Any]:
        """
        Dispatches raw transcript payload to FS-AI-Engine via HTTP with exponential backoff retries.
        """
        # Try full hierarchical intelligence endpoint first
        target_url = f"{self.ai_engine_url}/api/v1/transcript/process-full"
        fallback_url = f"{self.ai_engine_url}/api/v1/transcript/normalize"

        timeout_sec = float(getattr(self.settings, "ai_engine_timeout_seconds", 300.0))
        request_timeout = httpx.Timeout(connect=15.0, read=timeout_sec, write=30.0, pool=30.0)

        attempt = 0
        backoff = initial_backoff
        while attempt < max_retries:
            attempt += 1
            try:
                async with httpx.AsyncClient(timeout=request_timeout) as client:
                    resp = await client.post(target_url, json=payload, headers=self.engine_headers)
                    if resp.status_code == 404:
                        # Fallback to normalize endpoint if process-full is not mounted
                        resp = await client.post(fallback_url, json=payload, headers=self.engine_headers)

                if resp.status_code >= 500:
                    logger.warning(
                        f"FS-AI-Engine returned HTTP {resp.status_code} on attempt {attempt}/{max_retries}. Retrying in {backoff}s..."
                    )
                    await asyncio.sleep(backoff)
                    backoff *= 2.0
                    continue

                if resp.status_code not in [200, 201]:
                    raise PipelineOrchestratorError(
                        f"FS-AI-Engine rejected payload (HTTP {resp.status_code}): {resp.text}"
                    )

                return resp.json()

            except (httpx.TimeoutException, httpx.RequestError) as exc:
                err_detail = f"Timeout ({timeout_sec}s exceeded)" if isinstance(exc, httpx.TimeoutException) else str(exc)
                if attempt >= max_retries:
                    raise PipelineOrchestratorError(
                        f"Failed to communicate with FS-AI-Engine after {max_retries} attempts: {err_detail}"
                    ) from exc
                logger.warning(
                    f"Network error contacting FS-AI-Engine (attempt {attempt}/{max_retries}): {err_detail}. Retrying in {backoff}s..."
                )
                await asyncio.sleep(backoff)
                backoff *= 2.0

        raise PipelineOrchestratorError("Exceeded max retries contacting FS-AI-Engine.")

    async def orchestrate_meeting_ingestion(
        self,
        transcript_dict: dict[str, Any],
        user_id: str,
        project_id: str | None = None,
        custom_title: str | None = None,
    ) -> dict[str, Any]:
        """
        Execute full asynchronous background ingestion workflow:
        1. Content hash checksum verification (idempotency).
        2. HTTP transfer to FS-AI-Engine.
        3. Atomic 4-tier database persistence.

        Args:
            transcript_dict: Parsed transcript dict with id, title, sentences, etc.
            user_id: Account that owns the meeting (stamped on every row written).
            project_id: Target project identifier.
            custom_title: Optional title override.

        Returns:
            Dict containing processing status and summary metrics.
        """
        meeting_id = str(transcript_dict.get("id"))
        title = custom_title or transcript_dict.get("title") or f"Meeting {meeting_id}"
        date_str = transcript_dict.get("date")
        duration = float(transcript_dict.get("duration") or 0.0)
        sentences = transcript_dict.get("sentences", []) or []

        logger.info(f"Starting pipeline orchestration for meeting '{meeting_id}' ({len(sentences)} sentences)...")

        # 1. Compute Content Hash for Idempotency
        content_hash = self.compute_transcript_hash(sentences)

        # 2. Check Database for Idempotency Hit
        db: Session = SessionLocal()
        try:
            existing = get_owned_meeting(db, user_id, meeting_id)

            if existing and existing.content_hash == content_hash and existing.status == "PROCESSED":
                logger.info(f"Idempotency hit for meeting '{meeting_id}' (content_hash: {content_hash[:8]}). Skipping re-processing.")
                return {
                    "status": "success",
                    "meeting_id": meeting_id,
                    "is_idempotent_hit": True,
                    "title": existing.title,
                    "total_turns": len(existing.turns) if existing.turns else 0,
                    "total_topics": len(existing.topics) if existing.topics else 0,
                }
        finally:
            db.close()

        # 3. Dispatch to FS-AI-Engine via HTTP
        # Normalize sentence structures so speaker_id is always str for FS-AI-Engine schemas
        sanitized_sentences = []
        for s in sentences:
            sanitized_sentences.append({
                "index": int(s.get("index", 0)),
                "speaker_name": str(s.get("speaker_name") or "Unknown"),
                "speaker_id": str(s.get("speaker_id") if s.get("speaker_id") is not None else "0"),
                "text": str(s.get("text") or ""),
                "raw_text": str(s.get("raw_text") or s.get("text") or ""),
                "start_time": float(s.get("start_time") or 0.0),
                "end_time": float(s.get("end_time") or 0.0),
            })

        ai_payload = {
            "meeting_id": meeting_id,
            "title": title,
            "date": date_str,
            "duration": duration,
            "summary": transcript_dict.get("summary"),
            "sentences": sanitized_sentences,
        }

        ai_response = await self._dispatch_to_ai_engine(ai_payload)

        # Extract normalized turns, topics, and executive summary from AI Engine response
        level1_turns = ai_response.get("level1_turns") or ai_response.get("turns") or []
        level2_segments = ai_response.get("level2_segments") or ai_response.get("topics") or []
        level3_summary = ai_response.get("level3_summary") or {}

        # 1. AI Engine Level 3 intelligence overview (schema key: overview)
        ai_summary_text = (
            level3_summary.get("overview")
            or level3_summary.get("executive_overview")
            or ai_response.get("executive_summary")
            or ""
        ).strip()

        # 2. Fireflies native transcript summary
        ff_summary_raw = transcript_dict.get("summary")
        ff_summary_text = ""
        if isinstance(ff_summary_raw, dict):
            ff_summary_text = (
                ff_summary_raw.get("overview")
                or ff_summary_raw.get("shorthand_bullet")
                or ff_summary_raw.get("bullet_gist")
                or ff_summary_raw.get("gist")
                or ""
            ).strip()
        elif isinstance(ff_summary_raw, str):
            ff_summary_text = ff_summary_raw.strip()

        # Prioritize Fireflies overview, fallback to AI Engine summary
        summary_text = ff_summary_text or ai_summary_text or ""

        # 3b. Fill in any missing embeddings BEFORE opening the write transaction (no HTTP call while rows are held)
        missing_indices: list[int] = []
        texts_to_embed: list[str] = []
        for idx, t in enumerate(level1_turns):
            emb = t.get("embedding")
            if not (emb and len(emb) == 1536):
                missing_indices.append(idx)
                spk = str(t.get("speaker_name") or t.get("speaker") or "Unknown Speaker")
                txt = str(t.get("text") or "").strip()
                texts_to_embed.append(f"[{_fmt_ts(float(t.get('start_time', 0.0)))}] {spk}: {txt}")

        if missing_indices:
            logger.info(f"Generating missing embeddings for {len(texts_to_embed)} turns via AI Engine /embed endpoint...")
            try:
                async with httpx.AsyncClient(timeout=120.0) as client:
                    emb_resp = await client.post(
                        f"{self.ai_engine_url}/api/v1/transcript/embed",
                        json={"texts": texts_to_embed},
                        headers=self.engine_headers,
                    )
                emb_resp.raise_for_status()
                for m_idx, vec in zip(missing_indices, emb_resp.json().get("embeddings", []), strict=False):
                    if len(vec) == 1536:
                        level1_turns[m_idx]["embedding"] = vec
            except Exception as emb_err:
                raise PipelineOrchestratorError(f"Could not generate embeddings for meeting '{meeting_id}': {emb_err}") from emb_err
            still_missing = sum(1 for t in level1_turns if not (t.get("embedding") and len(t["embedding"]) == 1536))
            if still_missing:
                raise PipelineOrchestratorError(
                    f"{still_missing} of {len(level1_turns)} turns of meeting '{meeting_id}' have no embedding; not saving a half-searchable meeting."
                )

        # 4. Atomic 4-Tier Database Upsert
        db = SessionLocal()
        try:
            # Tier 1: Project
            if project_id:
                proj = claim_project(db, user_id, project_id)
                project_id = proj.id

            # Tier 2: Meeting
            meeting = get_owned_meeting(db, user_id, meeting_id)
            if not meeting:
                meeting = MeetingModel(
                    id=meeting_id,
                    user_id=user_id,
                    project_id=project_id,
                    title=title,
                    summary=summary_text,
                    content_hash=content_hash,
                    date=date_str,
                    duration=duration,
                    status="PROCESSED",
                )
                db.add(meeting)
                db.flush()
            else:
                # A retry of a FAILED ingestion must not keep the failure reason as its summary
                previous_summary = None if meeting.status == "FAILED" else meeting.summary
                meeting.project_id = project_id or meeting.project_id
                meeting.title = title or meeting.title
                meeting.summary = summary_text or previous_summary
                meeting.content_hash = content_hash
                meeting.date = date_str or meeting.date
                meeting.duration = duration or meeting.duration
                meeting.status = "PROCESSED"
                # Clean old topics and turns
                db.query(MeetingTopicModel).filter(MeetingTopicModel.meeting_id == meeting_id).delete()
                db.query(SpeakerTurnModel).filter(SpeakerTurnModel.meeting_id == meeting_id).delete()
                db.flush()

            # Tier 3: Meeting Topics
            topic_id_map: dict[int, str] = {}
            for seg in level2_segments:
                seg_idx = int(seg.get("segment_index") or seg.get("topic_index") or 1)
                t_id = str(uuid.uuid4())
                topic_id_map[seg_idx] = t_id
                topic_rec = MeetingTopicModel(
                    id=t_id,
                    user_id=user_id,
                    meeting_id=meeting_id,
                    topic_index=seg_idx,
                    title=seg.get("title") or seg.get("topic_name") or f"Topic {seg_idx}",
                    compiled_text=seg.get("compiled_text") or seg.get("topic_summary") or seg.get("summary") or "",
                    start_time=float(seg.get("start_time") or 0.0),
                    end_time=float(seg.get("end_time") or 0.0),
                )
                db.add(topic_rec)

            db.flush()

            # Tier 4: Speaker Turns with Embeddings and Rich Metadata
            turn_models = []
            for t in level1_turns:
                t_num = int(t.get("turn_number", 1))
                turn_id = str(t.get("turn_id") or f"mtg_{meeting_id}_turn_{t_num:03d}")
                assigned_topic_id = None
                if level2_segments:
                    # Match turn to topic segment by time window or segment_index
                    for seg in level2_segments:
                        s_start = float(seg.get("start_time", 0.0))
                        s_end = float(seg.get("end_time", 0.0))
                        t_start = float(t.get("start_time", 0.0))
                        if s_start <= t_start <= s_end:
                            assigned_topic_id = topic_id_map.get(
                                int(seg.get("segment_index") or seg.get("topic_index") or 1)
                            )
                            break

                spk_name = str(t.get("speaker_name") or t.get("speaker") or "Unknown Speaker")
                s_start = float(t.get("start_time", 0.0))
                s_end = float(t.get("end_time", 0.0))
                dur = float(t.get("duration", 0.0))
                is_q = bool(t.get("is_question", False))
                is_c = bool(t.get("is_commitment", False))
                is_b = bool(t.get("is_blocker", False))
                signals_list = t.get("signals") or []

                meta_dict = {
                    "speaker": spk_name,
                    "timestamp": _fmt_ts(s_start),
                    "meeting_id": str(meeting_id),
                    "project_id": str(project_id or "default-project"),
                    "signals": signals_list,
                    "is_question": is_q,
                    "is_commitment": is_c,
                    "is_blocker": is_b,
                }

                emb_vector = t.get("embedding")
                if not (emb_vector and len(emb_vector) == 1536):
                    emb_vector = None

                turn_rec = SpeakerTurnModel(
                    turn_id=turn_id,
                    user_id=user_id,
                    meeting_id=meeting_id,
                    topic_id=assigned_topic_id,
                    turn_number=t_num,
                    speaker_id=str(t.get("speaker_id") or t.get("speaker") or "spk_unknown"),
                    speaker_name=spk_name,
                    text=str(t.get("text", "")).strip(),
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
                turn_models.append(turn_rec)

            db.bulk_save_objects(turn_models)
            db.commit()
            logger.info(
                f"Successfully persisted meeting '{meeting_id}' ({len(level2_segments)} topics, {len(turn_models)} turns) to database."
            )

            return {
                "status": "success",
                "meeting_id": meeting_id,
                "is_idempotent_hit": False,
                "title": title,
                "total_turns": len(turn_models),
                "total_topics": len(level2_segments),
            }

        except Exception as exc:
            db.rollback()
            logger.error(f"Database persistence failure for meeting '{meeting_id}': {exc}", exc_info=True)
            raise PipelineOrchestratorError(f"Database error saving meeting '{meeting_id}': {exc!s}") from exc
        finally:
            db.close()

backend_pipeline_orchestrator = BackendPipelineOrchestrator()

def get_backend_pipeline_orchestrator() -> BackendPipelineOrchestrator:
    return backend_pipeline_orchestrator
