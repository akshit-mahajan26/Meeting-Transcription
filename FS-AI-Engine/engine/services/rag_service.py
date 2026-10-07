"""
FS-AI-Engine/services/rag_service.py
====================================
Universal Query & RAG Orchestration Pipeline.
Unified service layer processing ALL incoming user questions uniformly:
1. Query Embedding Generation
2. Vector Similarity Retrieval (pgvector + hybrid keyword scoring)
3. Context Block Assembly
4. Universal OpenRouter LLM Completion with strict conversational persona
5. Output Parsing & Structured Level 1 Citation Mapping
"""

import difflib
import math
import os
import re
import sys
import time
import logging
from typing import List, Optional, Dict, Any, Tuple, Set
from pathlib import Path
from pydantic import BaseModel, Field
from sqlalchemy import text

# Configure dedicated service logger
logger = logging.getLogger("fs-ai-engine")

# Resolve package paths dynamically
CURRENT_DIR = Path(__file__).resolve().parent
AI_ENGINE_ROOT = CURRENT_DIR.parent
if str(AI_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(AI_ENGINE_ROOT))

try:
    from engine.core.config import settings
    from engine.core.database import AsyncSessionLocal
    from engine.services.embedding_service import EmbeddingService
    from engine.services.llm_client import OpenRouterClient, get_openrouter_client
    from engine.services.local_reranker import local_reranker
except ModuleNotFoundError:
    from core.config import settings
    from core.database import AsyncSessionLocal
    from services.embedding_service import EmbeddingService
    from services.llm_client import OpenRouterClient, get_openrouter_client
    from services.local_reranker import local_reranker

try:
    from services.ingestion_service import (
        format_timestamp,
        enrich_chunk_text,
        prepare_chunk_for_embedding,
    )
except ImportError:
    from engine.services.ingestion_service import (
        format_timestamp,
        enrich_chunk_text,
        prepare_chunk_for_embedding,
    )

try:
    from graphs.chat_graph import run_chat_graph
except (ImportError, ModuleNotFoundError):
    try:
        from engine.graphs.chat_graph import run_chat_graph
    except (ImportError, ModuleNotFoundError):
        run_chat_graph = None

UNIVERSAL_RAG_SYSTEM_PROMPT = """You are an expert Meeting Intelligence Assistant. Your primary goal is to provide clear, professional, and conversational answers to *any* user query regarding ingested meeting transcripts (e.g., specific questions, summaries, action items, or participant involvement).

Guidelines:
1. **Conversational Synthesis:** Answer the user's question directly and naturally in the main response body. Never dump raw transcript blocks, speaker headers, or timestamps into the text response.
2. **Universal Applicability:** Whether the user asks about a person's involvement, a project decision, a timeline, or a general summary, synthesize the information cohesively.
3. **Strict Grounding:** Base your response exclusively on the provided transcript excerpts. If the information is missing from the context, state clearly that the records do not contain it."""

MEETING_STOP_WORDS = {
    "and", "or", "the", "in", "on", "at", "to", "for", "of", "with", "by", "from",
    "about", "into", "through", "after", "before", "between", "under", "above",
    "all", "any", "both", "each", "few", "more", "most", "other", "some", "such",
    "no", "nor", "not", "only", "own", "same", "so", "than", "too", "very",
    "can", "will", "just", "should", "now", "what", "which", "who", "whom",
    "this", "that", "these", "those", "am", "is", "are", "was", "were", "be",
    "been", "being", "have", "has", "had", "having", "do", "does", "did", "doing",
    "would", "could", "tell", "show", "give", "summarize", "summary", "notes",
    "point", "points", "key", "action", "item", "items", "decision", "decisions",
    "discussion", "discussions", "review", "sync", "call", "meeting", "meetings",
    "transcript", "transcripts", "overview", "details", "detail", "update", "updates",
    "hello", "hi", "hey", "direct", "test"
}

GENERIC_SPEAKER_WORDS = {
    "unknown", "speaker", "system", "user", "bot", "assistant"
}

STOP_WORDS = MEETING_STOP_WORDS


def is_name_token_match(token: str, candidate: str) -> bool:
    """Checks exact or fuzzy match for participant first/last names (e.g. dynamic name matching)."""
    t = token.lower()
    c = candidate.lower()
    if t == c:
        return True
    if len(t) >= 5 and len(c) >= 5:
        if difflib.SequenceMatcher(None, t, c).ratio() >= 0.82:
            return True
    return False


def resolve_speakers_from_query(
    query: str,
    speakers_db: List[str],
    meeting_tokens: Optional[Set[str]] = None,
) -> List[Tuple[str, str, str]]:
    """
    Identifies all specific participants dynamically mentioned in the query.
    Extracts participants directly from PostgreSQL speaker_turns and metadata.
    Returns a list of (resolved_speaker, matched_token, reasoning_log).
    """
    q_lower = query.lower()
    q_words = re.findall(r"[a-zA-Z]+", q_lower)
    found_speakers: List[Tuple[str, str, str]] = []
    seen = set()

    for spk in sorted(speakers_db, key=lambda s: len(s), reverse=True):
        spk_clean = spk.strip()
        if not spk_clean:
            continue
        # Remove trailing database generated UUIDs or hash IDs if present
        clean_name = re.sub(r"\b[A-Z0-9_]{10,}\b", "", spk_clean).strip()
        clean_lower = clean_name.lower()

        if not clean_lower or clean_lower in GENERIC_SPEAKER_WORDS:
            continue

        # Dynamic multi-word speaker match (handles any role or full name dynamically)
        if len(clean_lower) >= 4 and re.search(r"\b" + re.escape(clean_lower) + r"\b", q_lower):
            if spk_clean not in seen:
                seen.add(spk_clean)
                found_speakers.append((spk_clean, clean_lower, f"Detected participant '{spk_clean}'"))
            continue

        # Token level check with exact or fuzzy matching
        tokens = [p for p in re.findall(r"[a-zA-Z]+", clean_name) if len(p) >= 3 and p.lower() not in GENERIC_SPEAKER_WORDS]
        tokens_to_match = [
            t for t in tokens
            if not (meeting_tokens and t.lower() in meeting_tokens)
        ]
        for token in tokens_to_match:
            matched = False
            for qw in q_words:
                if is_name_token_match(qw, token):
                    if spk_clean not in seen:
                        seen.add(spk_clean)
                        found_speakers.append((spk_clean, qw, f"Detected participant '{qw}' -> '{spk_clean}'"))
                    matched = True
                    break
            if matched:
                break

    return found_speakers


def resolve_speaker_from_query(
    query: str,
    speakers_db: List[str],
    meeting_tokens: Optional[Set[str]] = None,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Single speaker resolution helper for backward compatibility."""
    res = resolve_speakers_from_query(query, speakers_db, meeting_tokens)
    if res:
        return res[0]
    return None, None, None


def resolve_meeting_from_query(
    query: str,
    meetings_db: List[Tuple[str, str]],
    matched_speaker_phrase: Optional[str] = None
) -> Tuple[Optional[str], Optional[str], str]:
    """
    Identifies if a specific meeting title or ID is mentioned in the query.
    Returns (resolved_meeting_id, resolved_meeting_title, reasoning_log).
    """
    q_lower = query.lower()
    
    # If the user explicitly asks a cross-meeting, comparative, or inventory query, DO NOT lock to a single meeting
    cross_meeting_pattern = r"\b(?:across (?:all |the |our )?meetings|in all meetings|all (?:the )?meetings|which meetings|what meetings|list (?:all )?meetings|in any meeting|any meetings|meetings that|meetings where|every meeting)\b"
    if re.search(cross_meeting_pattern, q_lower):
        return None, None, "Cross-meeting query: searching across all meetings"

    # If a speaker phrase matched, mask it out when detecting meeting title
    q_for_meeting = q_lower
    if matched_speaker_phrase:
        q_masked = re.sub(r"\b" + re.escape(matched_speaker_phrase) + r"\b", " ", q_lower)
        if any(w for w in re.findall(r"\w+", q_masked) if w not in MEETING_STOP_WORDS):
            q_for_meeting = q_masked

    # 1. Exact ID match (case-insensitive)
    for m_id, m_title in meetings_db:
        if m_id and m_id.lower() in q_lower:
            m_title_clean = (m_title or m_id).strip()
            return m_id, m_title_clean, f"Scoped to meeting '{m_title_clean}' ({m_id})"
            
    best_score = 0
    best_m_id = None
    best_m_title = None
    
    for m_id, m_title in meetings_db:
        if not m_title:
            continue
        m_title_clean = m_title.strip()
        m_title_lower = m_title_clean.lower()
        
        score = 0
        norm_title = re.sub(r"[^a-zA-Z0-9\s]", " ", m_title_lower)
        norm_title = " ".join(norm_title.split())
        var_title = " ".join([w for w in norm_title.split() if w not in {"and", "&"}])
        
        # Whole title match has highest precedence
        if re.search(r"\b" + re.escape(norm_title) + r"\b", q_for_meeting):
            score += 200 + len(norm_title)
        elif len(var_title.split()) > 1 and re.search(r"\b" + re.escape(var_title) + r"\b", q_for_meeting):
            score += 150 + len(var_title)
        else:
            title_tokens = [w for w in re.findall(r"[a-zA-Z0-9_\-]+", m_title_lower) if w not in MEETING_STOP_WORDS]
            if title_tokens:
                matched_distinctive = [t for t in title_tokens if re.search(r"\b" + re.escape(t) + r"\b", q_for_meeting)]
                if matched_distinctive:
                    match_ratio = len(matched_distinctive) / len(title_tokens)
                    # Require at least 2 distinctive tokens or high coverage (>= 70%) to avoid hijacking by a single keyword
                    if len(matched_distinctive) >= 2 or match_ratio >= 0.7:
                        score = int(60 * match_ratio) + sum(len(t) for t in matched_distinctive)
                    
        if score > best_score:
            best_score = score
            best_m_id = m_id
            best_m_title = m_title_clean
            
    if best_score >= 40:
        return best_m_id, best_m_title, f"Scoped to meeting '{best_m_title}' ({best_m_id})"
        
    return None, None, "Universal semantic vector search across meeting transcripts"


# ============================================================================
# Schemas
# ============================================================================

class CitationModel(BaseModel):
    speaker: str
    start_time: float
    end_time: float
    meeting_id: str
    exact_raw_text: str
    topic_context: Optional[str] = "Level 1 Turn"


class RetrievedTurn(BaseModel):
    turn_id: str
    meeting_id: str
    speaker: str
    start_time: float
    end_time: float
    text: str
    topic_context: str
    similarity_score: float = 0.0


class RAGQueryResponse(BaseModel):
    query: str
    answer: str
    citations: List[CitationModel] = Field(default_factory=list)
    citations_count: int = 0
    provenance_verified: bool = True
    routing_mode: str = "MICRO"
    routing_confidence: float = 1.0
    routing_reasoning: str = ""
    llm_model_used: str = getattr(settings, "LLM_MODEL_NAME", "gemini-2.5-flash")


# ============================================================================
# Helper Functions
# ============================================================================

def format_timestamp(seconds: float) -> str:
    """Formats second offsets into MM:SS format."""
    mins = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{mins:02d}:{secs:02d}"


# ============================================================================
# Universal RAG Orchestrator Function
# ============================================================================

async def process_user_query(
    query: str,
    project_id: Optional[str] = None,
    meeting_id: Optional[str] = None,
    speaker: Optional[str] = None,
    top_k: int = 5,
) -> RAGQueryResponse:
    """
    Universal Query Orchestration Pipeline:
    Executes uniform RAG across all query types (summaries, action items,
    participant inquiries, pricing terms, security reviews).
    """
    overall_start = time.time()
    clean_query = query.strip()
    query_lower = clean_query.lower()

    logger.info("=" * 70)
    logger.info(
        f"[UNIVERSAL RAG] Query: '{clean_query}' | "
        f"Project Scope: {project_id} | Meeting Scope: {meeting_id} | Speaker Scope: {speaker}"
    )

    # ------------------------------------------------------------------------
    # LangGraph StateGraph Execution (Primary Orchestration Flow)
    # ------------------------------------------------------------------------
    if run_chat_graph is not None:
        try:
            graph_res = await run_chat_graph(
                query=clean_query,
                project_id=project_id,
                meeting_id=meeting_id,
                speaker=speaker,
                top_k=top_k,
            )
            citations = [
                CitationModel(
                    speaker=c.get("speaker", "Unknown"),
                    start_time=float(c.get("start_time", 0.0)),
                    end_time=float(c.get("end_time", 0.0)),
                    meeting_id=c.get("meeting_id", ""),
                    exact_raw_text=c.get("exact_raw_text", ""),
                    topic_context=c.get("topic_context", "Level 1 Turn"),
                )
                for c in graph_res.get("citations", [])
            ]
            return RAGQueryResponse(
                query=clean_query,
                answer=graph_res.get("answer", ""),
                citations=citations,
                citations_count=len(citations),
                provenance_verified=graph_res.get("provenance_verified", True),
                routing_mode=graph_res.get("routing_mode", "LANGGRAPH_RAG"),
                routing_confidence=1.0,
                routing_reasoning=graph_res.get("reasoning", "LangGraph StateGraph Execution"),
                llm_model_used=graph_res.get("llm_model_used", getattr(settings, "LLM_MODEL_NAME", "gemini-2.5-flash")),
            )
        except Exception as e:
            logger.warning(f"[LANGGRAPH ROUTING] Error in LangGraph execution: {e}. Executing native RAG flow.")

    # ------------------------------------------------------------------------
    # STEP 1: Query Embedding Generation
    # ------------------------------------------------------------------------
    embed_start = time.time()
    embedding_service = EmbeddingService()
    query_vectors = await embedding_service.generate_embeddings([clean_query])
    query_vector = query_vectors[0] if query_vectors else []
    embed_latency_ms = (time.time() - embed_start) * 1000

    logger.info(
        f"[STEP 1: EMBEDDING] Model: {embedding_service.model} | "
        f"Dim: {len(query_vector)} | Latency: {embed_latency_ms:.2f}ms"
    )

    # ------------------------------------------------------------------------
    # STEP 2: Vector Similarity & Hybrid Retrieval (pgvector)
    # ------------------------------------------------------------------------
    retrieval_start = time.time()
    retrieved_turns: List[RetrievedTurn] = []
    reasoning = "Universal semantic vector search across meeting transcripts"

    # Tokenize meaningful keywords for hybrid boost
    tokens = re.findall(r"[a-zA-Z0-9_\-]+", query_lower)
    meaningful_keywords = [w for w in tokens if len(w) > 2 and w not in STOP_WORDS]
    query_tokens_set = set(meaningful_keywords)

    resolved_meeting_id = meeting_id
    resolved_speaker = speaker
    relevant_meetings: List[Dict[str, Any]] = []

    if AsyncSessionLocal is not None:
        async with AsyncSessionLocal() as session:
            # 1. Fetch available meetings
            m_res = await session.execute(text("SELECT id, title FROM meetings"))
            meetings_db = m_res.fetchall()
            meetings_dict = {m_id: (m_title or m_id).strip() for m_id, m_title in meetings_db}

            # Dynamically derive meeting title tokens from database records
            db_meeting_tokens = {
                w.lower()
                for _, title in meetings_db
                if title
                for w in re.findall(r"[a-zA-Z0-9_\-]+", title)
                if len(w) > 2 and w.lower() not in MEETING_STOP_WORDS
            }

            # 2. Dynamic Speaker Resolution if not explicitly scoped
            matched_spk_phrase = None
            resolved_speakers: List[str] = [speaker] if speaker else []
            if not resolved_speakers:
                spk_res = await session.execute(
                    text("SELECT DISTINCT COALESCE(metadata->>'speaker', speaker_name) FROM speaker_turns WHERE speaker_name IS NOT NULL OR metadata->>'speaker' IS NOT NULL")
                )
                speakers_db = [r[0] for r in spk_res.fetchall() if r[0]]
                found_speakers = resolve_speakers_from_query(clean_query, speakers_db, db_meeting_tokens)
                if found_speakers:
                    resolved_speakers = [s[0] for s in found_speakers]
                    resolved_speaker = resolved_speakers[0]
                    matched_spk_phrase = " ".join(s[1] for s in found_speakers)
                    for s in found_speakers:
                        logger.info(f"[SPEAKER RESOLUTION] {s[2]}")

            # 3. Dynamic Meeting Resolution if not explicitly scoped
            if resolved_meeting_id:
                m_title = meetings_dict.get(resolved_meeting_id, resolved_meeting_id)
                reasoning = f"Scoped to meeting '{m_title}' ({resolved_meeting_id})"
            else:
                m_id, m_title, meeting_reasoning = resolve_meeting_from_query(clean_query, meetings_db, matched_spk_phrase)
                if m_id:
                    resolved_meeting_id = m_id
                    reasoning = meeting_reasoning
                elif resolved_speakers:
                    spk_clauses = []
                    params = {}
                    for idx, s in enumerate(resolved_speakers):
                        p_name = f"s_param_{idx}"
                        spk_clauses.append(f"(t.metadata->>'speaker' ILIKE :{p_name} OR LOWER(t.speaker_name) LIKE :{p_name})")
                        params[p_name] = f"%{s.lower()}%"

                    spk_sql = (
                        "SELECT m.id, m.title, count(t.turn_id) as turn_count "
                        "FROM meetings m "
                        "JOIN speaker_turns t ON m.id = t.meeting_id "
                        f"WHERE {' OR '.join(spk_clauses)} "
                        "GROUP BY m.id, m.title "
                        "ORDER BY turn_count DESC"
                    )
                    m_spk_res = await session.execute(text(spk_sql), params)
                    m_candidates = m_spk_res.fetchall()
                    if m_candidates:
                        relevant_meetings = [
                            {"id": r[0], "title": (r[1] or r[0]).strip(), "turns": r[2]}
                            for r in m_candidates
                        ]
                        is_involvement_query = any(w in clean_query.lower() for w in ["meeting", "meetings", "involved", "attend", "attended", "present", "which", "where", "who", "all", "list", "part of"])
                        if len(relevant_meetings) == 1 and not is_involvement_query:
                            resolved_meeting_id = relevant_meetings[0]["id"]
                            reasoning = f"Scoped to meeting '{relevant_meetings[0]['title']}' ({resolved_meeting_id}) based on participant: {', '.join(resolved_speakers)}"
                        else:
                            # Multiple meetings or cross-meeting inquiry: DO NOT restrict to a single meeting!
                            m_titles_summary = ", ".join(f"'{m['title']}'" for m in relevant_meetings)
                            reasoning = f"Found {len(relevant_meetings)} meetings for {', '.join(resolved_speakers)}: {m_titles_summary}"
                    else:
                        reasoning = "Universal semantic vector search across meeting transcripts"
                else:
                    reasoning = "Universal semantic vector search across meeting transcripts"

            if resolved_speakers and "Scoped to meeting" in reasoning and "based on participant" not in reasoning:
                reasoning += f" | Filtered to speakers: {', '.join(resolved_speakers)}"
            elif resolved_speakers and "Scoped to meeting" not in reasoning:
                reasoning += f" (Participants: {', '.join(resolved_speakers)})"
            logger.info(f"[SCOPE RESOLUTION] {reasoning}")

            # 2c. Build Two-Stage Scalable Hybrid Retrieval:
            # Stage 1: Reciprocal Rank Fusion (pgvector Top 50 + Postgres FTS GIN Top 50 -> Top 30 in SQL)
            has_vector = bool(query_vector and len(query_vector) == 1536)
            vec_str = "[" + ",".join(str(x) for x in query_vector) + "]" if has_vector else "[]"

            base_filters = ["1=1"]
            params: Dict[str, Any] = {
                "has_vector": has_vector,
                "q_vec": vec_str,
                "q_text": clean_query,
            }

            if resolved_meeting_id:
                base_filters.append("(t.metadata->>'meeting_id' = :m_id OR t.meeting_id = :m_id)")
                params["m_id"] = resolved_meeting_id

            if project_id:
                base_filters.append("(t.metadata->>'project_id' = :p_id OR m.project_id = :p_id)")
                params["p_id"] = project_id

            if resolved_speaker:
                spk_parts = [p.strip() for p in re.findall(r"\w+", resolved_speaker) if len(p.strip()) >= 3 and p.lower() not in STOP_WORDS]
                spk_clauses = [
                    "(t.metadata->>'speaker' ILIKE :spk_full OR LOWER(t.speaker_name) LIKE :spk_full)"
                ]
                params["spk_full"] = f"%{resolved_speaker.lower()}%"
                for idx, part in enumerate(spk_parts):
                    p_name = f"spk_token_{idx}"
                    spk_clauses.append(f"(t.metadata->>'speaker' ILIKE :{p_name} OR LOWER(t.speaker_name) LIKE :{p_name})")
                    params[p_name] = f"%{part.lower()}%"
                base_filters.append(f"({' OR '.join(spk_clauses)})")

            where_clause = " AND ".join(base_filters)

            # Build Stage 1 RRF Hybrid SQL with CTEs
            def build_rrf_query(where_str: str) -> str:
                return (
                    f"WITH dense_search AS (\n"
                    f"    SELECT t.turn_id, \n"
                    f"           ROW_NUMBER() OVER (\n"
                    f"               ORDER BY CASE WHEN t.embedding IS NOT NULL AND :has_vector = true \n"
                    f"                             THEN (t.embedding <=> CAST(:q_vec AS vector)) \n"
                    f"                             ELSE 1.0 END ASC, t.start_time ASC\n"
                    f"           ) as dense_rank\n"
                    f"    FROM speaker_turns t\n"
                    f"    LEFT JOIN meetings m ON t.meeting_id = m.id\n"
                    f"    WHERE {where_str} AND (:has_vector = false OR t.embedding IS NOT NULL)\n"
                    f"    ORDER BY CASE WHEN t.embedding IS NOT NULL AND :has_vector = true \n"
                    f"                  THEN (t.embedding <=> CAST(:q_vec AS vector)) \n"
                    f"                  ELSE 1.0 END ASC, t.start_time ASC\n"
                    f"    LIMIT 50\n"
                    f"),\n"
                    f"lexical_search AS (\n"
                    f"    SELECT t.turn_id, \n"
                    f"           ROW_NUMBER() OVER (\n"
                    f"               ORDER BY ts_rank_cd(\n"
                    f"                   to_tsvector('english', coalesce(t.speaker_name, '') || ' ' || coalesce(t.text, '')),\n"
                    f"                   plainto_tsquery('english', :q_text)\n"
                    f"               ) DESC, t.start_time ASC\n"
                    f"           ) as lex_rank\n"
                    f"    FROM speaker_turns t\n"
                    f"    LEFT JOIN meetings m ON t.meeting_id = m.id\n"
                    f"    WHERE {where_str}\n"
                    f"      AND to_tsvector('english', coalesce(t.speaker_name, '') || ' ' || coalesce(t.text, '')) @@ plainto_tsquery('english', :q_text)\n"
                    f"    ORDER BY ts_rank_cd(\n"
                    f"        to_tsvector('english', coalesce(t.speaker_name, '') || ' ' || coalesce(t.text, '')),\n"
                    f"        plainto_tsquery('english', :q_text)\n"
                    f"    ) DESC, t.start_time ASC\n"
                    f"    LIMIT 50\n"
                    f")\n"
                    f"SELECT \n"
                    f"    t.turn_id, t.meeting_id,\n"
                    f"    COALESCE(t.metadata->>'speaker', t.speaker_name, 'Unknown') as speaker_name,\n"
                    f"    t.text, t.start_time, t.end_time,\n"
                    f"    COALESCE(t.metadata->>'timestamp', to_char(to_timestamp(t.start_time), 'HH24:MI:SS')) as timestamp_str,\n"
                    f"    t.metadata,\n"
                    f"    m.title as meeting_title, top.title as topic_title,\n"
                    f"    CASE WHEN t.embedding IS NOT NULL AND :has_vector = true \n"
                    f"         THEN (1 - (t.embedding <=> CAST(:q_vec AS vector))) \n"
                    f"         ELSE 0.0 END as vector_sim,\n"
                    f"    (\n"
                    f"        COALESCE(1.0 / (60.0 + d.dense_rank), 0.0) +\n"
                    f"        COALESCE(1.0 / (60.0 + l.lex_rank), 0.0)\n"
                    f"    ) as rrf_score\n"
                    f"FROM dense_search d\n"
                    f"FULL OUTER JOIN lexical_search l ON d.turn_id = l.turn_id\n"
                    f"JOIN speaker_turns t ON COALESCE(d.turn_id, l.turn_id) = t.turn_id\n"
                    f"LEFT JOIN meetings m ON t.meeting_id = m.id\n"
                    f"LEFT JOIN meeting_topics top ON t.topic_id = top.id\n"
                    f"ORDER BY rrf_score DESC\n"
                    f"LIMIT 30"
                )

            # Execution with resilient fallback
            try:
                rrf_sql = build_rrf_query(where_clause)
                res = await session.execute(text(rrf_sql), params)
                rows = res.fetchall()

                # Fallback if filtered by speaker returned 0 rows
                if not rows and resolved_speaker:
                    logger.info(f"[RETRIEVAL FALLBACK] Zero rows for speaker filter '{resolved_speaker}'. Retrying without speaker constraint.")
                    fallback_filters = [f for f in base_filters if "speaker" not in f and "speaker_name" not in f]
                    fallback_where = " AND ".join(fallback_filters)
                    fallback_params = {k: v for k, v in params.items() if not k.startswith("spk_")}
                    fallback_sql = build_rrf_query(fallback_where)
                    res = await session.execute(text(fallback_sql), fallback_params)
                    rows = res.fetchall()
            except Exception as sql_err:
                logger.warning(f"[RETRIEVAL] RRF CTE query encountered an issue ({sql_err}); falling back to standard indexed query.")
                simple_parts = [
                    "SELECT t.turn_id, t.meeting_id, ",
                    "COALESCE(t.metadata->>'speaker', t.speaker_name, 'Unknown') as speaker_name, ",
                    "t.text, t.start_time, t.end_time, ",
                    "COALESCE(t.metadata->>'timestamp', to_char(to_timestamp(t.start_time), 'HH24:MI:SS')) as timestamp_str, ",
                    "t.metadata, ",
                    "m.title as meeting_title, top.title as topic_title, ",
                    "CASE WHEN t.embedding IS NOT NULL AND :has_vector = true ",
                    "     THEN (1 - (t.embedding <=> CAST(:q_vec AS vector))) ",
                    "     ELSE 0.0 END as vector_sim, 0.0 as rrf_score ",
                    "FROM speaker_turns t ",
                    "LEFT JOIN meetings m ON t.meeting_id = m.id ",
                    "LEFT JOIN meeting_topics top ON t.topic_id = top.id ",
                    f"WHERE {where_clause}"
                ]
                simple_sql = " ".join(simple_parts)
                if has_vector:
                    simple_sql += " ORDER BY vector_sim DESC, t.start_time ASC LIMIT 30"
                else:
                    simple_sql += " ORDER BY t.start_time ASC LIMIT 30"
                res = await session.execute(text(simple_sql), params)
                rows = res.fetchall()

            # Format raw candidate records for Stage 2 Reranking
            candidate_records = []
            for r in rows:
                t_id, m_id, spk, txt, start_t, end_t, ts_str, meta_json, m_title, top_title, vec_sim = (
                    r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10]
                )
                rrf_score = r[11] if len(r) > 11 else 0.0
                txt_raw = txt or ""
                candidate_records.append({
                    "turn_id": t_id or f"{m_id}_turn",
                    "meeting_id": m_id or "",
                    "speaker": spk or "Unknown",
                    "start_time": float(start_t or 0.0),
                    "end_time": float(end_t or 0.0),
                    "text": txt_raw,
                    "timestamp_str": ts_str or "00:00:00",
                    "topic_context": top_title or m_title or "Level 1 Turn",
                    "vector_sim": float(vec_sim or 0.0),
                    "rrf_score": float(rrf_score or 0.0),
                })

            # Stage 2: In-Process FlashRank Cross-Encoder Reranking
            reranked_records = local_reranker.rerank(clean_query, candidate_records, top_k=len(candidate_records))

            candidates = []
            for item in reranked_records:
                rerank_score = item.get("rerank_score", 0.0)
                base_score = rerank_score if rerank_score > 0 else (item.get("rrf_score", 0.0) * 10.0 + item.get("vector_sim", 0.0))

                # Participant boost if speaker matches
                if resolved_speaker:
                    spk_low = (item.get("speaker") or "").lower()
                    for token in resolved_speaker.lower().split():
                        if len(token) >= 3 and token in spk_low:
                            base_score += 0.25
                            break

                candidates.append(RetrievedTurn(
                    turn_id=item["turn_id"],
                    meeting_id=item["meeting_id"],
                    speaker=item["speaker"],
                    start_time=item["start_time"],
                    end_time=item["end_time"],
                    text=item["text"],
                    topic_context=item["topic_context"],
                    similarity_score=round(base_score, 4)
                ))

            # Group candidates by meeting_id to detect multi-meeting breadth
            distinct_meetings: Dict[str, List[RetrievedTurn]] = {}
            for c in candidates:
                if c.meeting_id:
                    distinct_meetings.setdefault(c.meeting_id, []).append(c)

            is_list_query = bool(re.search(r"\b(?:list (?:all )?meetings|what meetings|which meetings|all the meetings|all meetings|how many meetings|meeting list|available meetings)\b", clean_query.lower()))

            # If candidates span multiple distinct meetings and no single meeting_id was strictly locked down:
            if is_list_query or (len(distinct_meetings) > 1 and not resolved_meeting_id):
                # Multi-Meeting Stratified Selection:
                # Guarantee fair representation across all matching meetings (prevents single-meeting concentration / starvation)
                selected_turns: List[RetrievedTurn] = []
                num_meetings = len(distinct_meetings)
                per_meeting_quota = 3 if num_meetings <= 4 else 2
                max_total_chunks = min(15, max(6, num_meetings * per_meeting_quota))

                # Round 1: Top turn from each meeting
                for m_id, m_turns in distinct_meetings.items():
                    m_turns.sort(key=lambda x: x.similarity_score, reverse=True)
                    if m_turns:
                        selected_turns.append(m_turns[0])

                # Round 2: Additional top turns from each meeting up to quota
                for m_id, m_turns in distinct_meetings.items():
                    for turn in m_turns[1:per_meeting_quota]:
                        if len(selected_turns) >= max_total_chunks:
                            break
                        if turn not in selected_turns:
                            selected_turns.append(turn)

                # Fill any remaining quota with highest scoring overall
                candidates.sort(key=lambda x: x.similarity_score, reverse=True)
                for c in candidates:
                    if len(selected_turns) >= max_total_chunks:
                        break
                    if c not in selected_turns:
                        selected_turns.append(c)

                selected_turns.sort(key=lambda x: (x.meeting_id, x.start_time))
                retrieved_turns = selected_turns
            else:
                candidates.sort(key=lambda x: x.similarity_score, reverse=True)
                top_turns = candidates[:top_k] if candidates else []
                top_turns.sort(key=lambda x: x.start_time)
                retrieved_turns = top_turns

    retrieval_latency_ms = (time.time() - retrieval_start) * 1000
    scores = [t.similarity_score for t in retrieved_turns] if retrieved_turns else [0.0]
    logger.info(
        f"[STEP 2: VECTOR RETRIEVAL] Matches Found: {len(retrieved_turns)} across {len(set(t.meeting_id for t in retrieved_turns))} meetings | "
        f"Score Range: [{min(scores):.2f} - {max(scores):.2f}] | Latency: {retrieval_latency_ms:.2f}ms"
    )

    # ------------------------------------------------------------------------
    # STEP 3: Context Assembly
    # ------------------------------------------------------------------------
    if not retrieved_turns:
        logger.warning("[STEP 3: CONTEXT] Zero matching excerpts found in vector database.")
        return RAGQueryResponse(
            query=clean_query,
            answer="Based on the ingested meeting records, no relevant discussions or transcript excerpts were found matching your inquiry.",
            citations=[],
            citations_count=0,
            provenance_verified=False,
            routing_reasoning="Zero matching speaker turns found",
            llm_model_used=getattr(settings, "LLM_MODEL_NAME", "gemini-2.5-flash")
        )

    context_lines = []
    for idx, t in enumerate(retrieved_turns):
        time_label = f"{format_timestamp(t.start_time)} - {format_timestamp(t.end_time)}"
        context_lines.append(
            f"[{idx + 1}] Speaker: {t.speaker} ({time_label}) | Meeting: {t.meeting_id} | Topic: {t.topic_context}\n"
            f"Excerpt: \"{t.text}\""
        )
    formatted_context = "\n\n".join(context_lines)

    extra_record_context = ""
    list_instruction = ""

    if relevant_meetings and resolved_speakers:
        spk_label = ", ".join(resolved_speakers)
        mtg_bullets = "\n".join(
            f"- **{m['title']}** (ID: {m['id']}, {m.get('turns', 0)} turns)"
            for m in relevant_meetings
        )
        extra_record_context = (
            f"Database Records - Verified Meetings with Participant(s) {spk_label} ({len(relevant_meetings)} total):\n"
            f"{mtg_bullets}\n\n---\n"
        )
        list_instruction = (
            f"- The user is asking about meetings involving {spk_label}. You MUST explicitly list and mention ALL {len(relevant_meetings)} verified meetings ({', '.join(m['title'] for m in relevant_meetings)}) from the Database Records above in your answer, providing relevant context or quotes for each.\n"
        )
    elif is_list_query and meetings_db:
        mtg_list_str = "\n".join(f"- {(m_title or m_id).strip()} (ID: {m_id})" for m_id, m_title in meetings_db)
        extra_record_context = f"Database Records - Complete List of All Ingested Meetings in the System ({len(meetings_db)} total):\n{mtg_list_str}\n\n---\n"
        list_instruction = f"- The user is asking to list all meetings. You MUST list all {len(meetings_db)} meetings from the Database Records above with their titles and IDs.\n"

    formatted_context = extra_record_context + formatted_context

    logger.info(f"[STEP 3: CONTEXT] Formatted {len(retrieved_turns)} turn blocks for prompt assembly.")

    # ------------------------------------------------------------------------
    # STEP 4: Universal OpenRouter LLM Completion
    # ------------------------------------------------------------------------
    llm = get_openrouter_client()
    target_model = llm.default_model

    user_prompt = (
        f"User Question: {clean_query}\n\n"
        f"Verified Meeting Transcript Context:\n{formatted_context}\n\n"
        f"Instructions:\n"
        f"{list_instruction}"
        f"- Please provide your conversational answer addressing the user's question directly based strictly on the above context."
    )

    messages = [
        {"role": "system", "content": UNIVERSAL_RAG_SYSTEM_PROMPT.strip()},
        {"role": "user", "content": user_prompt}
    ]

    llm_start = time.time()
    logger.info(
        f"[STEP 4: OPENROUTER LLM] Target Model: {target_model} | "
        f"Context Excerpts: {len(retrieved_turns)} | System Prompt: {len(UNIVERSAL_RAG_SYSTEM_PROMPT)} chars"
    )

    answer_text = await llm.generate_chat_completion(
        messages=messages,
        model=target_model,
        max_tokens=4096,
        temperature=0.2
    )
    llm_latency_ms = (time.time() - llm_start) * 1000

    logger.info(
        f"[STEP 4: LLM COMPLETION] Status: SUCCESS | Model: {target_model} | "
        f"Latency: {llm_latency_ms:.2f}ms | Answer Length: {len(answer_text)} chars"
    )

    # ------------------------------------------------------------------------
    # STEP 5: Output Parsing & Level 1 Citation Mapping
    # ------------------------------------------------------------------------
    citations = [
        CitationModel(
            speaker=t.speaker,
            start_time=t.start_time,
            end_time=t.end_time,
            meeting_id=t.meeting_id,
            exact_raw_text=t.text,
            topic_context=t.topic_context
        )
        for t in retrieved_turns
    ]

    total_latency_ms = (time.time() - overall_start) * 1000
    logger.info(
        f"[STEP 5: DISPATCH] Query fulfilled with {len(citations)} citations in {total_latency_ms:.2f}ms"
    )
    logger.info("=" * 70)

    return RAGQueryResponse(
        query=clean_query,
        answer=answer_text.strip(),
        citations=citations,
        citations_count=len(citations),
        provenance_verified=True,
        routing_mode="MICRO",
        routing_confidence=1.0,
        routing_reasoning=reasoning,
        llm_model_used=target_model
    )
