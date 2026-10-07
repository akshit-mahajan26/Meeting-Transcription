import hashlib
import json
import logging
import uuid
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from ..core.config import settings, Settings
from ..models.schemas import SpeakerTurn, RawTranscriptPayload
from ..models.db_models import Meeting, MeetingTopic, MeetingTurn, Project
from .chunking import TokenAwareChunker
from .boundary_detector import SemanticBoundaryDetector, TopicSegmentData
from .embedding_service import EmbeddingService
from .normalizer import SpeakerNormalizer

logger = logging.getLogger(__name__)


class PipelineExecutionResult(BaseModel):
    """Output payload from full Phase 3 pipeline execution."""
    meeting_id: str
    content_hash: str
    is_idempotent_hit: bool = False
    executive_summary: str
    total_topics: int
    total_turns: int
    topics: List[Dict[str, Any]] = Field(default_factory=list)


class PipelineOrchestrator:
    """
    Phase 3 Pipeline Orchestrator.
    
    1. Computes cryptographic content hash checksums for incoming meeting payloads (Idempotency).
    2. Executes token-aware chunking and sub-splitting (400-token guardrail + sentence overlap).
    3. Detects semantic boundaries & topics via cosine similarity and LLM validation.
    4. Compiles Level 3 executive summary.
    5. Executes an ACID-compliant database transaction across 4 relational tiers:
       - meetings (Level 3 Summary, content_hash, status)
       - meeting_topics (Level 2 Topic Chapters, captured topic_ids)
       - meeting_turns (Level 1 Speaker Turns with PGVector embeddings & enterprise metadata).
    """

    def __init__(
        self,
        config: Optional[Settings] = None,
        chunker: Optional[TokenAwareChunker] = None,
        boundary_detector: Optional[SemanticBoundaryDetector] = None,
        embedding_service: Optional[EmbeddingService] = None,
    ):
        self.config = config or settings
        self.normalizer = SpeakerNormalizer()
        self.embedding_service = embedding_service or EmbeddingService(config=self.config)
        self.chunker = chunker or TokenAwareChunker(config=self.config)
        self.boundary_detector = boundary_detector or SemanticBoundaryDetector(
            config=self.config,
            embedding_service=self.embedding_service,
        )

    @staticmethod
    def compute_content_hash(meeting_id: str, turns: List[SpeakerTurn]) -> str:
        """
        Calculate deterministic SHA-256 checksum of meeting transcript payload
        to guarantee idempotency on retries and duplicate webhooks.
        """
        hasher = hashlib.sha256()
        hasher.update(meeting_id.strip().encode("utf-8"))
        for t in sorted(turns, key=lambda x: (x.start_time, x.turn_number)):
            line = f"{t.speaker_name}:{t.start_time}:{t.end_time}:{t.text}"
            hasher.update(line.encode("utf-8"))
        return hasher.hexdigest()


    async def process_transcript(
        self,
        payload: RawTranscriptPayload,
        session: Optional[AsyncSession] = None,
        project_tag: Optional[str] = None,
    ) -> PipelineExecutionResult:
        """
        Normalize raw transcript sentences into SpeakerTurns and execute
        the complete ingestion, chunking, boundary detection, and persistence pipeline.
        """
        norm_result = self.normalizer.normalize(payload)
        turns = norm_result.turns
        return await self.execute_pipeline(
            meeting_id=payload.meeting_id,
            turns=turns,
            title=payload.title,
            project_tag=project_tag,
            session=session,
        )

    async def execute_pipeline(
        self,
        meeting_id: str,
        turns: List[SpeakerTurn],
        title: Optional[str] = None,
        project_tag: Optional[str] = None,
        session: Optional[AsyncSession] = None,
    ) -> PipelineExecutionResult:
        """
        Execute full Phase 3 hierarchy, embedding, and persistence workflow.

        Args:
            meeting_id: Unique meeting identifier.
            turns: Initial normalized Level 1 speaker turns.
            title: Optional meeting title.
            project_tag: Enterprise project tag or identifier.
            session: Optional async database session for ACID transaction.

        Returns:
            PipelineExecutionResult containing summary, topics, and execution metrics.
        """
        if not turns:
            return PipelineExecutionResult(
                meeting_id=meeting_id,
                content_hash="",
                is_idempotent_hit=False,
                executive_summary="Empty meeting transcript.",
                total_topics=0,
                total_turns=0,
                topics=[],
            )

        # 1. Compute Content Hash for Idempotency
        content_hash = self.compute_content_hash(meeting_id, turns)

        # 2. Idempotency Check in Database (if session provided)
        if session:
            existing_meeting = await self._check_existing_meeting(session, meeting_id, content_hash)
            if existing_meeting:
                logger.info(f"Idempotent hit for meeting '{meeting_id}' with hash {content_hash}. Returning existing.")
                return PipelineExecutionResult(
                    meeting_id=meeting_id,
                    content_hash=content_hash,
                    is_idempotent_hit=True,
                    executive_summary=existing_meeting.summary or "",
                    total_topics=len(existing_meeting.topics) if existing_meeting.topics else 0,
                    total_turns=len(existing_meeting.turns) if existing_meeting.turns else 0,
                    topics=[{"topic_index": t.topic_index, "title": t.title} for t in (existing_meeting.topics or [])],
                )

        # 3. Token-Aware Chunking & Sub-Splitter (400 tokens threshold + 1-2 sentence overlap)
        chunked_turns = self.chunker.process_turns(meeting_id, turns)

        # 4. Semantic Boundary Detection, Topic Generation & Level 3 Executive Summary
        topics_data, executive_summary, embeddings = await self.boundary_detector.detect_boundaries_and_generate_topics(
            meeting_id=meeting_id,
            turns=chunked_turns,
            title=title,
        )

        # Map turns to their embeddings by turn_id
        embedding_map: Dict[str, List[float]] = {}
        for idx, t in enumerate(chunked_turns):
            if idx < len(embeddings):
                embedding_map[t.turn_id] = embeddings[idx]

        # 5. ACID-Compliant Database Persistence (if session provided)
        if session:
            await self._persist_hierarchy(
                session=session,
                meeting_id=meeting_id,
                title=title,
                project_tag=project_tag,
                content_hash=content_hash,
                executive_summary=executive_summary,
                topics_data=topics_data,
                embedding_map=embedding_map,
                turns=chunked_turns,
            )

        # Prepare response payload
        topics_summary = [
            {
                "topic_index": td.topic_index,
                "title": td.title,
                "start_time": td.start_time,
                "end_time": td.end_time,
                "turns_count": len(td.turns),
                "compiled_text_preview": td.compiled_text[:200] + ("..." if len(td.compiled_text) > 200 else ""),
            }
            for td in topics_data
        ]

        return PipelineExecutionResult(
            meeting_id=meeting_id,
            content_hash=content_hash,
            is_idempotent_hit=False,
            executive_summary=executive_summary,
            total_topics=len(topics_data),
            total_turns=len(chunked_turns),
            topics=topics_summary,
        )

    async def _check_existing_meeting(
        self,
        session: AsyncSession,
        meeting_id: str,
        content_hash: str,
    ) -> Optional[Meeting]:
        """Query for existing meeting with matching content hash."""
        stmt = select(Meeting).where(
            Meeting.id == meeting_id,
            Meeting.content_hash == content_hash,
        )
        result = await session.execute(stmt)
        return result.scalars().first()

    async def _persist_hierarchy(
        self,
        session: AsyncSession,
        meeting_id: str,
        title: Optional[str],
        project_tag: Optional[str],
        content_hash: str,
        executive_summary: str,
        topics_data: List[TopicSegmentData],
        embedding_map: Dict[str, List[float]],
        turns: List[SpeakerTurn],
    ) -> None:
        """
        Execute ACID transaction inserting or updating records across the 4-tier schema:
        1. projects (ensures project_tag exists if provided)
        2. meetings (Level 3 Executive Summary + content_hash)
        3. meeting_topics (Level 2 Topic Segments, capturing generated topic_ids)
        4. meeting_turns (Level 1 Speaker Turns with PGVector embeddings).
        """
        async with session.begin():
            # 1. Upsert Project if project_tag is supplied
            if project_tag:
                proj_stmt = select(Project).where(Project.id == project_tag)
                proj_res = await session.execute(proj_stmt)
                proj = proj_res.scalars().first()
                if not proj:
                    new_proj = Project(id=project_tag, name=f"Project {project_tag}")
                    session.add(new_proj)
                    await session.flush()

            # 2. Clean out old meeting records on re-processing
            del_stmt = delete(Meeting).where(Meeting.id == meeting_id)
            await session.execute(del_stmt)
            await session.flush()

            # 3. Insert Meeting record (Tier 2) with Level 3 Executive Summary
            meeting_rec = Meeting(
                id=meeting_id,
                project_id=project_tag,
                title=title or f"Meeting {meeting_id}",
                summary=executive_summary,
                content_hash=content_hash,
                status="PROCESSED",
                duration=round(turns[-1].end_time - turns[0].start_time, 2) if turns else 0.0,
            )
            session.add(meeting_rec)
            await session.flush()

            # 4. Insert meeting_topics (Tier 3) and capture generated topic_id foreign keys
            for topic_data in topics_data:
                topic_id = str(uuid.uuid4())
                topic_rec = MeetingTopic(
                    id=topic_id,
                    meeting_id=meeting_id,
                    topic_index=topic_data.topic_index,
                    title=topic_data.title,
                    compiled_text=topic_data.compiled_text,
                    start_time=topic_data.start_time,
                    end_time=topic_data.end_time,
                )
                session.add(topic_rec)
                await session.flush()

                # 5. Insert meeting_turns (Tier 4) associated with parent topic_id
                def _fmt_ts(sec: Optional[float]) -> str:
                    s = max(0, int(round(float(sec or 0))))
                    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"

                for turn in topic_data.turns:
                    vector = embedding_map.get(turn.turn_id)
                    metadata_payload = {
                        "speaker": turn.speaker_name or "Unknown",
                        "timestamp": _fmt_ts(turn.start_time),
                        "meeting_id": str(meeting_id or ""),
                        "project_id": str(project_tag or "default-project"),
                        "is_question": turn.is_question,
                        "is_commitment": turn.is_commitment,
                        "is_blocker": turn.is_blocker,
                        "signals": turn.signals,
                        "sentence_indices": turn.sentence_indices,
                    }
                    turn_rec = MeetingTurn(
                        id=turn.turn_id,
                        meeting_id=meeting_id,
                        topic_id=topic_id,
                        turn_number=turn.turn_number,
                        speaker=turn.speaker_name,
                        speaker_id=turn.speaker_id,
                        text=turn.text,
                        start_time=turn.start_time,
                        end_time=turn.end_time,
                        project_tag=project_tag,
                        vector=vector,
                        meta_data=metadata_payload,
                    )
                    session.add(turn_rec)

            # Flush all records inside the active transaction block
            await session.flush()

        logger.info(
            f"Successfully persisted meeting '{meeting_id}' ({len(topics_data)} topics, {len(turns)} turns) with PGVector."
        )
