import math
import re
import hashlib
import logging
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field

from ..core.config import settings
from ..core.database import db_manager
from .agent_router import RoutingDecision

logger = logging.getLogger(__name__)


class EnrichedTurnContext(BaseModel):
    """
    Enriched Level 1 Atomic Turn Context.
    Combines verbatim Level 1 text with resolved parent Level 2 Topic context and meeting metadata.
    """
    turn_id: str
    meeting_id: str
    project_id: Optional[str] = None
    speaker: str
    start_time: float
    end_time: float
    text: str
    raw_text: str  # Verbatim text for the Provable Citations Contract
    similarity_score: float
    # Parent Level 2 Topic Context (Resolved via Pointer Store Foreign Key)
    topic_id: Optional[str] = None
    topic_name: Optional[str] = None
    topic_summary: Optional[str] = None
    # Parent Meeting Context
    meeting_title: Optional[str] = None
    meeting_date: Optional[str] = None


class MicroRetrievalResult(BaseModel):
    """Result of Metadata Pre-Filtered Hybrid Vector Search with Pointer Resolution."""
    query: str
    embedding_model_used: str
    turns: List[EnrichedTurnContext] = Field(default_factory=list)
    total_turns: int
    retrieval_mode: str = "MICRO_VECTOR_POINTER_STORE"


class VectorEmbeddingService:
    """
    Generates high-dimensional vector representations for queries and documents
    configured dynamically via settings.EMBEDDING_MODEL_NAME and settings.EMBEDDING_DIMENSION.
    """

    def __init__(self, model_name: Optional[str] = None, dimension: Optional[int] = None):
        self.model_name = model_name or settings.EMBEDDING_MODEL_NAME
        self.dimension = dimension or settings.EMBEDDING_DIMENSION

    def encode(self, text: str) -> List[float]:
        """
        Converts text into normalized dense embedding vector.
        Uses deterministic hashing and projection invariant to Python seeds.
        """
        tokens = re.findall(r"\b[a-zA-Z0-9_]+\b", text.lower())
        vec = [0.0] * self.dimension
        if not tokens:
            return vec

        for t in tokens:
            h = int(hashlib.md5(t.encode("utf-8")).hexdigest()[:8], 16)
            pos = h % self.dimension
            sign = 1.0 if (h >> 3) % 2 == 0 else -1.0
            vec[pos] += sign * (len(t) ** 0.5)

        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [round(v / norm, 5) for v in vec]
        return vec


class MicroRetriever:
    """
    Hybrid Vector & Pointer Store Service.
    Converts query to embeddings, applies metadata pre-filtering, executes vector similarity search,
    and resolves parent Level 2 topic pointers and Level 3 meeting metadata via foreign keys.
    """

    def __init__(
        self,
        embedding_service: Optional[VectorEmbeddingService] = None,
        search_limit: Optional[int] = None,
        similarity_threshold: Optional[float] = None
    ):
        self.embedding_service = embedding_service or VectorEmbeddingService()
        self.search_limit = search_limit or settings.SEARCH_LIMIT
        self.similarity_threshold = similarity_threshold or settings.SIMILARITY_THRESHOLD

    async def execute_micro_retrieval(self, decision: RoutingDecision) -> MicroRetrievalResult:
        """
        Executes metadata pre-filtered vector retrieval and pointer resolution.
        """
        query = decision.query
        filters = decision.filters
        logger.info(
            f"[MicroRetriever] Executing vector retrieval for query='{query}' "
            f"using model '{self.embedding_service.model_name}' (dim={self.embedding_service.dimension})"
        )

        # 1. Vector Generation
        query_vector = self.embedding_service.encode(query)

        # 2. Metadata Pre-Filtered Search & Cosine Distance Scoring
        candidates = await self._search_turns(
            query=query,
            query_vector=query_vector,
            speaker_filter=filters.speaker,
            project_id_filter=filters.project_id
        )

        # 3. Pointer Store Resolution (Resolve Parent Level 2 Topics & Meeting Metadata)
        enriched_turns = await self._resolve_pointers(candidates)

        logger.info(f"[MicroRetriever] Retrieved and resolved {len(enriched_turns)} enriched Level 1 turns.")
        return MicroRetrievalResult(
            query=query,
            embedding_model_used=self.embedding_service.model_name,
            turns=enriched_turns,
            total_turns=len(enriched_turns)
        )

    async def _search_turns(
        self,
        query: str,
        query_vector: List[float],
        speaker_filter: Optional[str] = None,
        project_id_filter: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Applies metadata pre-filtering and sorts by vector cosine similarity.
        """
        mem = db_manager.get_in_memory_records()
        candidates = []

        query_tokens = set(re.findall(r"\b[a-zA-Z0-9_]{3,}\b", query.lower()))

        for t_id, turn_data in mem["turns"].items():
            # 1. Metadata Pre-Filtering: Speaker
            if speaker_filter:
                speaker_actual = turn_data.get("speaker", "").lower()
                if speaker_filter.lower() not in speaker_actual:
                    continue

            # 1b. Metadata Pre-Filtering: Project ID via meeting relation
            meeting_id = turn_data.get("meeting_id")
            meeting_data = mem["meetings"].get(meeting_id, {})
            if project_id_filter:
                if meeting_data.get("project_id") != project_id_filter:
                    continue

            # 2. Vector Cosine Similarity
            t_vec = turn_data.get("embedding")
            if not t_vec:
                t_vec = self.embedding_service.encode(turn_data.get("text", ""))

            # Compute Cosine Similarity
            dot_product = sum(a * b for a, b in zip(query_vector, t_vec))
            norm_q = math.sqrt(sum(a * a for a in query_vector)) or 1.0
            norm_t = math.sqrt(sum(b * b for b in t_vec)) or 1.0
            cosine_sim = dot_product / (norm_q * norm_t)

            # Keyword lexical overlap boost
            turn_tokens = set(re.findall(r"\b[a-zA-Z0-9_]{3,}\b", turn_data.get("text", "").lower()))
            overlap = len(query_tokens.intersection(turn_tokens))
            hybrid_score = cosine_sim + (overlap * 0.15)

            candidates.append({
                "turn": turn_data,
                "meeting": meeting_data,
                "score": round(hybrid_score, 4)
            })

        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates[:self.search_limit]

    async def _resolve_pointers(self, candidates: List[Dict[str, Any]]) -> List[EnrichedTurnContext]:
        """
        Relational Pointer Store Resolution:
        Follows foreign key pointers from Level 1 turn -> Level 2 topic -> Level 3 meeting.
        """
        mem = db_manager.get_in_memory_records()
        enriched = []

        for item in candidates:
            t = item["turn"]
            m = item["meeting"]
            score = item["score"]

            topic_id = t.get("topic_id")
            topic_data = mem["topics"].get(topic_id, {}) if topic_id else {}

            enriched.append(EnrichedTurnContext(
                turn_id=t.get("id", t.get("turn_id", "")),
                meeting_id=t.get("meeting_id", ""),
                project_id=m.get("project_id"),
                speaker=t.get("speaker", "Unknown"),
                start_time=float(t.get("start_time", 0.0)),
                end_time=float(t.get("end_time", 0.0)),
                text=t.get("text", ""),
                raw_text=t.get("raw_text", t.get("text", "")),
                similarity_score=score,
                topic_id=topic_id,
                topic_name=topic_data.get("topic_name"),
                topic_summary=topic_data.get("summary"),
                meeting_title=m.get("title"),
                meeting_date=str(m.get("meeting_date")) if m.get("meeting_date") else None
            ))

        return enriched
