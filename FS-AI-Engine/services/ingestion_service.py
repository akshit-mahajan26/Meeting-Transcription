"""
FS-AI-Engine/services/ingestion_service.py
=========================================
Chunking & Vector Embedding Preparation Pipeline.

Enriches Fireflies transcript turns prior to vector embedding by prefixing
each chunk with its structured metadata context:
    f"Speaker: {speaker} | Timestamp: {timestamp} | Content: {text}"

Guarantees:
1. Semantic and speaker identity capture within pgvector embeddings.
2. Structured metadata storage in pgvector JSONB column:
   - speaker: String (e.g., dynamic speaker name from transcript)
   - timestamp: String (e.g., "00:14:22")
   - meeting_id: String
   - project_id: String (for scope filtering)
3. Direct compatibility with PostgreSQL pgvector (speaker_turns / meeting_turns).
"""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("fs-ai-engine.ingestion")


# ============================================================================
# Timestamp & Enrichment Helpers
# ============================================================================

def format_timestamp(seconds: Union[float, int, str, None]) -> str:
    """
    Normalizes second offsets or raw time strings into HH:MM:SS format.
    Example: 862.0 -> "00:14:22"
    """
    if seconds is None:
        return "00:00:00"

    if isinstance(seconds, str):
        clean_s = seconds.strip()
        if ":" in clean_s:
            parts = clean_s.split(":")
            if len(parts) == 2:
                return f"00:{parts[0].zfill(2)}:{parts[1].zfill(2)}"
            elif len(parts) == 3:
                return f"{parts[0].zfill(2)}:{parts[1].zfill(2)}:{parts[2].zfill(2)}"
        try:
            seconds = float(clean_s)
        except ValueError:
            return "00:00:00"

    total_sec = max(0, int(round(float(seconds or 0))))
    hrs = total_sec // 3600
    mins = (total_sec % 3600) // 60
    secs = total_sec % 60
    return f"{hrs:02d}:{mins:02d}:{secs:02d}"


def enrich_chunk_text(speaker: str, timestamp: str, text: str) -> str:
    """
    Prefixes transcript chunk with speaker and timestamp metadata context.
    Standardized format:
      f"Speaker: {speaker} | Timestamp: {timestamp} | Content: {text}"
    """
    clean_speaker = (speaker or "Unknown").strip()
    clean_timestamp = (timestamp or "00:00:00").strip()
    clean_text = (text or "").strip()
    return f"Speaker: {clean_speaker} | Timestamp: {clean_timestamp} | Content: {clean_text}"


def prepare_chunk_for_embedding(
    speaker: str,
    timestamp: Union[float, int, str, None],
    text: str,
    meeting_id: Optional[str] = None,
    project_id: Optional[str] = None,
    **extra_metadata: Any,
) -> Dict[str, Any]:
    """
    Prepares a single transcript turn for embedding and pgvector persistence.

    Returns:
        Dict with keys:
          - 'enriched_text': Enriched string for embedding generation
          - 'raw_text': Verbatim speech text for citation cards
          - 'metadata': Structured JSONB payload containing speaker, timestamp, meeting_id, project_id
    """
    formatted_ts = format_timestamp(timestamp)
    clean_speaker = (speaker or "Unknown").strip()
    clean_text = (text or "").strip()
    enriched_str = enrich_chunk_text(clean_speaker, formatted_ts, clean_text)

    metadata_payload = {
        "speaker": clean_speaker,
        "timestamp": formatted_ts,
        "meeting_id": str(meeting_id or ""),
        "project_id": str(project_id or "default-project"),
        **extra_metadata,
    }

    return {
        "enriched_text": enriched_str,
        "raw_text": clean_text,
        "metadata": metadata_payload,
    }


# ============================================================================
# Schemas
# ============================================================================

class EnrichedChunkPayload(BaseModel):
    turn_id: str
    meeting_id: str
    project_id: str = "default-project"
    speaker: str
    timestamp: str
    start_time: float = 0.0
    end_time: float = 0.0
    raw_text: str
    enriched_text: str
    metadata: Dict[str, Any] = Field(default_factory=dict)
    embedding: Optional[List[float]] = None


# ============================================================================
# Ingestion Service Class
# ============================================================================

class IngestionService:
    """
    Orchestrates transcript chunking, text enrichment, embedding generation,
    and structured pgvector metadata storage.
    """

    def __init__(self, embedding_service: Any = None):
        if embedding_service is not None:
            self.embedding_service = embedding_service
        else:
            try:
                from engine.services.embedding_service import EmbeddingService
                self.embedding_service = EmbeddingService()
            except Exception:
                self.embedding_service = None

    def prepare_turns(
        self,
        turns: List[Any],
        meeting_id: str,
        project_id: Optional[str] = None,
    ) -> List[EnrichedChunkPayload]:
        """
        Transforms raw speaker turns (dicts or Pydantic models) into enriched chunk payloads
        ready for vector embedding and pgvector storage.
        """
        eff_project_id = project_id or "default-project"
        prepared_chunks: List[EnrichedChunkPayload] = []

        for idx, turn in enumerate(turns):
            # Extract fields whether dict or object
            if isinstance(turn, dict):
                speaker = turn.get("speaker_name") or turn.get("speaker") or "Unknown"
                raw_text = turn.get("text") or ""
                start_t = float(turn.get("start_time") or 0.0)
                end_t = float(turn.get("end_time") or start_t)
                t_id = turn.get("turn_id") or turn.get("id") or f"{meeting_id}_turn_{idx+1:03d}"
            else:
                speaker = getattr(turn, "speaker_name", getattr(turn, "speaker", "Unknown"))
                raw_text = getattr(turn, "text", "")
                start_t = float(getattr(turn, "start_time", 0.0) or 0.0)
                end_t = float(getattr(turn, "end_time", start_t) or start_t)
                t_id = getattr(turn, "turn_id", getattr(turn, "id", f"{meeting_id}_turn_{idx+1:03d}"))

            chunk_info = prepare_chunk_for_embedding(
                speaker=speaker,
                timestamp=start_t,
                text=raw_text,
                meeting_id=meeting_id,
                project_id=eff_project_id,
                turn_number=idx + 1,
            )

            prepared_chunks.append(
                EnrichedChunkPayload(
                    turn_id=t_id,
                    meeting_id=meeting_id,
                    project_id=eff_project_id,
                    speaker=chunk_info["metadata"]["speaker"],
                    timestamp=chunk_info["metadata"]["timestamp"],
                    start_time=start_t,
                    end_time=end_t,
                    raw_text=chunk_info["raw_text"],
                    enriched_text=chunk_info["enriched_text"],
                    metadata=chunk_info["metadata"],
                )
            )

        return prepared_chunks

    async def generate_and_store_embeddings(
        self,
        session: AsyncSession,
        chunks: List[EnrichedChunkPayload],
    ) -> int:
        """
        Generates vector embeddings for enriched texts and persists vectors + structured
        metadata JSONB into PostgreSQL pgvector (speaker_turns).
        """
        if not chunks:
            return 0

        enriched_texts = [c.enriched_text for c in chunks]
        logger.info(f"[INGESTION] Generating embeddings for {len(enriched_texts)} enriched chunks.")

        vectors: List[List[float]] = []
        if self.embedding_service:
            vectors = await self.embedding_service.generate_embeddings(enriched_texts)

        # Ensure vector length matches chunks
        if len(vectors) < len(chunks):
            # Fallback deterministic vectors
            from engine.services.micro_retriever import VectorEmbeddingService
            fallback_vec_svc = VectorEmbeddingService(dimension=1536)
            vectors = [fallback_vec_svc.encode(t) for t in enriched_texts]

        # Upsert into PostgreSQL speaker_turns
        upsert_sql = text("""
            INSERT INTO speaker_turns (
                turn_id, meeting_id, speaker_name, text, start_time, end_time,
                duration, turn_number, embedding, metadata
            ) VALUES (
                :turn_id, :meeting_id, :speaker_name, :text, :start_time, :end_time,
                :duration, :turn_number, CAST(:embedding AS vector), CAST(:metadata AS jsonb)
            )
            ON CONFLICT (turn_id) DO UPDATE SET
                speaker_name = EXCLUDED.speaker_name,
                text = EXCLUDED.text,
                start_time = EXCLUDED.start_time,
                end_time = EXCLUDED.end_time,
                duration = EXCLUDED.duration,
                embedding = EXCLUDED.embedding,
                metadata = EXCLUDED.metadata;
        """)

        persisted_count = 0
        for idx, chunk in enumerate(chunks):
            vec = vectors[idx]
            vec_str = "[" + ",".join(str(x) for x in vec) + "]"
            meta_json = json.dumps(chunk.metadata)

            duration = max(0.5, round(chunk.end_time - chunk.start_time, 2))

            await session.execute(
                upsert_sql,
                {
                    "turn_id": chunk.turn_id,
                    "meeting_id": chunk.meeting_id,
                    "speaker_name": chunk.speaker,
                    "text": chunk.raw_text,
                    "start_time": chunk.start_time,
                    "end_time": chunk.end_time,
                    "duration": duration,
                    "turn_number": idx + 1,
                    "embedding": vec_str,
                    "metadata": meta_json,
                },
            )
            persisted_count += 1

        logger.info(f"[INGESTION] Persisted {persisted_count} turns into pgvector with enriched embeddings and metadata.")
        return persisted_count
