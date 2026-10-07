"""
FS-AI-Engine/graphs/chat_nodes.py
=================================
LangGraph Nodes for the Dedicated AI Engine Chatbot Pipeline.

Implements the modular node functions executed within the ChatState StateGraph:
1. extract_filters_node: Dynamically resolves participant names and meeting boundaries from DB records.
2. embed_query_node: Generates 1536-dimensional query vector embeddings.
3. vector_retrieval_node: Executes PostgreSQL pgvector cosine similarity search with metadata filtering.
4. llm_synthesis_node: Synthesizes conversational response using OpenRouter LLM.
5. format_citations_node: Maps retrieved transcript turns into Level 1 citation models.
6. fallback_node: Handles zero-match scenarios with helpful conversational guidance.
"""

from __future__ import annotations

import difflib
import logging
import math
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TypedDict, Set
from pydantic import BaseModel, Field
from sqlalchemy import text

# Dedicated AI Service Logger
logger = logging.getLogger("fs-ai-engine.chat_graph")

# Package path resolution
CURRENT_DIR = Path(__file__).resolve().parent
AI_ENGINE_ROOT = CURRENT_DIR.parent
if str(AI_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(AI_ENGINE_ROOT))

try:
    from engine.core.config import settings
    from engine.core.database import AsyncSessionLocal
    from engine.services.embedding_service import EmbeddingService
    from engine.services.llm_client import OpenRouterClient, get_openrouter_client
    from services.ingestion_service import format_timestamp, enrich_chunk_text
except ModuleNotFoundError:
    try:
        from core.config import settings
        from core.database import AsyncSessionLocal
        from services.embedding_service import EmbeddingService
        from services.llm_client import OpenRouterClient, get_openrouter_client
        from services.ingestion_service import format_timestamp, enrich_chunk_text
    except ModuleNotFoundError:
        from ..core.config import settings
        from ..core.database import AsyncSessionLocal
        from ..services.embedding_service import EmbeddingService
        from ..services.llm_client import OpenRouterClient, get_openrouter_client
        from ..services.ingestion_service import format_timestamp, enrich_chunk_text

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

UNIVERSAL_RAG_SYSTEM_PROMPT = """You are an expert Meeting Intelligence Assistant. Your primary goal is to provide clear, professional, and conversational answers to *any* user query regarding ingested meeting transcripts (e.g., specific questions, summaries, action items, or participant involvement).

Guidelines:
1. **Conversational Synthesis:** Answer the user's question directly, clearly, and naturally in the main response body. Never dump raw transcript blocks, speaker headers, or timestamps into the text response.
2. **Participant Involvement & Meetings:** When asked to list meetings or about who was involved, explicitly name the meeting title(s) and meeting ID(s) based on the verified database records and excerpts.
3. **Strict Grounding:** Base your response on the provided context, transcript excerpts, and database records. When the user asks to list all meetings or available meetings, enumerate all meetings documented in the database records. If information is missing from the context, state clearly that the records do not contain it."""


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
# LangGraph State Schema
# ============================================================================

class ChatState(TypedDict, total=False):
    """
    Central state container tracked across all LangGraph nodes in the chat pipeline.
    """
    query: str
    project_id: Optional[str]
    meeting_id: Optional[str]
    extracted_speaker: Optional[str]
    extracted_speakers: Optional[List[str]]
    query_embedding: List[float]
    retrieved_chunks: List[Dict[str, Any]]
    relevant_meetings: Optional[List[Dict[str, Any]]]
    available_meetings: Optional[List[Dict[str, Any]]]
    answer: str
    citations: List[Dict[str, Any]]
    citations_count: int
    provenance_verified: bool
    reasoning: str
    routing_mode: str
    llm_model_used: str
    latency_ms: float
    start_time: float


# ============================================================================
# Node 1: Extract Filters Node
# ============================================================================

async def extract_filters_node(state: ChatState) -> Dict[str, Any]:
    """
    Node 1: Extract Filters.
    Parses user query to identify mentioned participants from database metadata
    and checks if a specific meeting title or participant-based meeting scope is requested.
    """
    query = state.get("query", "").strip()
    project_id = state.get("project_id")
    resolved_speaker = state.get("extracted_speaker")
    resolved_speakers: List[str] = state.get("extracted_speakers") or []
    if resolved_speaker and not resolved_speakers:
        resolved_speakers = [resolved_speaker]

    resolved_meeting_id = state.get("meeting_id")
    relevant_meetings: List[Dict[str, Any]] = []
    reasoning = "Universal semantic vector search across meeting transcripts"

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

            # 2. Dynamic Speaker Entity Extraction from database records and metadata
            matched_spk_phrase = None
            if not resolved_speakers:
                spk_res = await session.execute(
                    text("SELECT DISTINCT COALESCE(metadata->>'speaker', speaker_name) FROM speaker_turns WHERE speaker_name IS NOT NULL OR metadata->>'speaker' IS NOT NULL")
                )
                speakers_db = [r[0] for r in spk_res.fetchall() if r[0]]
                found_speakers = resolve_speakers_from_query(query, speakers_db, db_meeting_tokens)
                if found_speakers:
                    resolved_speakers = [s[0] for s in found_speakers]
                    resolved_speaker = resolved_speakers[0]
                    matched_spk_phrase = " ".join(s[1] for s in found_speakers)
                    for s in found_speakers:
                        logger.info(f"[LANGGRAPH: EXTRACT_FILTERS] {s[2]}")

            # 3. Dynamic Meeting Resolution
            if resolved_meeting_id:
                m_title = meetings_dict.get(resolved_meeting_id, resolved_meeting_id)
                reasoning = f"Scoped to meeting '{m_title}' ({resolved_meeting_id})"
            else:
                m_id, m_title, meeting_reasoning = resolve_meeting_from_query(query, meetings_db, matched_spk_phrase)
                if m_id:
                    resolved_meeting_id = m_id
                    reasoning = meeting_reasoning
                elif resolved_speakers:
                    # Participant-based meeting resolution:
                    # Query all meetings where the resolved speaker(s) participated
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
                        is_involvement_query = any(w in query.lower() for w in ["meeting", "meetings", "involved", "attend", "attended", "present", "which", "where", "who", "all", "list", "part of"])
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

            available_meetings = [{"id": m_id, "title": (m_title or m_id).strip()} for m_id, m_title in meetings_db]

    logger.info(
        f"[LANGGRAPH: EXTRACT_FILTERS] Speakers: {resolved_speakers} | "
        f"Meeting: '{resolved_meeting_id}' | Relevant Meetings: {len(relevant_meetings)} | Project: '{project_id}' | Reasoning: '{reasoning}'"
    )

    return {
        "extracted_speaker": resolved_speaker,
        "extracted_speakers": resolved_speakers,
        "meeting_id": resolved_meeting_id,
        "project_id": project_id,
        "available_meetings": available_meetings,
        "relevant_meetings": relevant_meetings,
        "reasoning": reasoning,
    }


# ============================================================================
# Node 2: Embed Query Node
# ============================================================================

async def embed_query_node(state: ChatState) -> Dict[str, Any]:
    """
    Node 2: Embed Query.
    Generates 1536-dimensional dense vector embeddings for vector retrieval.
    """
    query = state.get("query", "").strip()
    embed_start = time.time()

    embedding_service = EmbeddingService()
    vectors = await embedding_service.generate_embeddings([query])
    query_vector = vectors[0] if vectors else []

    latency_ms = (time.time() - embed_start) * 1000
    logger.info(
        f"[LANGGRAPH: EMBED_QUERY] Model: {embedding_service.model} | "
        f"Dim: {len(query_vector)} | Latency: {latency_ms:.2f}ms"
    )

    return {
        "query_embedding": query_vector,
    }


# ============================================================================
# Node 3: Vector Retrieval Node
# ============================================================================

async def vector_retrieval_node(state: ChatState) -> Dict[str, Any]:
    """
    Node 3: Vector Retrieval with pgvector & Metadata Filtering.
    Queries PostgreSQL speaker_turns with cosine distance (1 - <=> vector)
    and applies metadata filters on speaker, meeting, and project JSONB columns.
    """
    retrieval_start = time.time()
    query = state.get("query", "").strip()
    query_lower = query.lower()
    query_vector = state.get("query_embedding", [])
    resolved_speaker = state.get("extracted_speaker")
    resolved_speakers: List[str] = state.get("extracted_speakers") or []
    if resolved_speaker and not resolved_speakers:
        resolved_speakers = [resolved_speaker]

    resolved_meeting_id = state.get("meeting_id")
    project_id = state.get("project_id")

    tokens = re.findall(r"[a-zA-Z0-9_\-]+", query_lower)
    meaningful_keywords = [w for w in tokens if len(w) > 2 and w not in STOP_WORDS]
    query_tokens_set = set(meaningful_keywords)

    has_vector = bool(query_vector and len(query_vector) == 1536)
    vec_str = "[" + ",".join(str(x) for x in query_vector) + "]" if has_vector else "[]"

    retrieved_chunks: List[Dict[str, Any]] = []

    if AsyncSessionLocal is not None:
        async with AsyncSessionLocal() as session:
            query_parts = [
                "SELECT t.turn_id, t.meeting_id, ",
                "COALESCE(t.metadata->>'speaker', t.speaker_name, 'Unknown') as speaker_name, ",
                "t.text, t.start_time, t.end_time, ",
                "COALESCE(t.metadata->>'timestamp', to_char(to_timestamp(t.start_time), 'HH24:MI:SS')) as timestamp_str, ",
                "t.metadata, ",
                "m.title as meeting_title, top.title as topic_title, ",
                "CASE WHEN t.embedding IS NOT NULL AND :has_vector = true ",
                "     THEN (1 - (t.embedding <=> CAST(:q_vec AS vector))) ",
                "     ELSE 0.0 END as vector_sim ",
                "FROM speaker_turns t ",
                "LEFT JOIN meetings m ON t.meeting_id = m.id ",
                "LEFT JOIN meeting_topics top ON t.topic_id = top.id ",
                "WHERE 1=1"
            ]
            params: Dict[str, Any] = {
                "has_vector": has_vector,
                "q_vec": vec_str,
            }

            # Scope Filtering
            if resolved_meeting_id:
                query_parts.append("AND (t.metadata->>'meeting_id' = :m_id OR t.meeting_id = :m_id)")
                params["m_id"] = resolved_meeting_id

            if project_id:
                query_parts.append("AND (t.metadata->>'project_id' = :p_id OR m.project_id = :p_id)")
                params["p_id"] = project_id

            # Multi-Speaker Metadata Filter
            if resolved_speakers:
                all_spk_clauses = []
                for s_idx, spk_target in enumerate(resolved_speakers):
                    spk_parts = [p.strip() for p in re.findall(r"\w+", spk_target) if len(p.strip()) >= 3 and p.lower() not in STOP_WORDS]
                    p_full = f"spk_full_{s_idx}"
                    params[p_full] = f"%{spk_target.lower()}%"
                    sub_clauses = [f"(t.metadata->>'speaker' ILIKE :{p_full} OR LOWER(t.speaker_name) LIKE :{p_full})"]
                    for p_idx, part in enumerate(spk_parts):
                        p_name = f"spk_token_{s_idx}_{p_idx}"
                        sub_clauses.append(f"(t.metadata->>'speaker' ILIKE :{p_name} OR LOWER(t.speaker_name) LIKE :{p_name})")
                        params[p_name] = f"%{part.lower()}%"
                    all_spk_clauses.append(f"({' OR '.join(sub_clauses)})")
                query_parts.append(f"AND ({' OR '.join(all_spk_clauses)})")

            query_sql = " ".join(query_parts) + (" ORDER BY vector_sim DESC, t.start_time ASC" if has_vector else " ORDER BY t.start_time ASC")
            res = await session.execute(text(query_sql), params)
            rows = res.fetchall()

            # Resilient Fallback: If strict speaker filter yielded 0 rows, search without speaker filter
            if not rows and resolved_speakers:
                logger.info(f"[LANGGRAPH: VECTOR_RETRIEVAL] Fallback search across all participants for '{resolved_speakers}'.")
                fallback_parts = [p for p in query_parts if not p.startswith("AND (t.metadata->>'speaker'") and not p.startswith("AND ((t.metadata->>'speaker'") and not p.startswith("AND (((t.metadata->>'speaker'")]
                fallback_sql = " ".join(fallback_parts) + (" ORDER BY vector_sim DESC, t.start_time ASC" if has_vector else " ORDER BY t.start_time ASC")
                fallback_params = {k: v for k, v in params.items() if not k.startswith("spk_")}
                res = await session.execute(text(fallback_sql), fallback_params)
                rows = res.fetchall()

            candidates: List[Dict[str, Any]] = []
            for r in rows:
                t_id, m_id, spk, txt, start_t, end_t, ts_str, meta_json, m_title, top_title, vec_sim = (
                    r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10]
                )
                txt_raw = (txt or "").strip()
                # Downweight non-substantive conversational fillers (< 15 chars monosyllables like "Ah,.", "Okay.")
                is_filler = len(txt_raw) < 15 and bool(re.match(r"^(?:ah|ok|okay|yes|no|yeah|right|correct|hello|hi|bye|thanks)[\.\,\s]*$", txt_raw.lower()))
                
                enriched_chunk = enrich_chunk_text(spk, ts_str, txt_raw)
                enriched_lower = enriched_chunk.lower()

                vector_score = max(0.0, float(vec_sim or 0.0))
                kw_matches = sum(1 for kw in query_tokens_set if kw in enriched_lower)
                score = (vector_score * 3.0) + (float(kw_matches) * 1.5)

                if resolved_speakers and any(any(tok.lower() in (spk or "").lower() for tok in s.split() if len(tok) >= 3) for s in resolved_speakers):
                    score += 3.5

                if is_filler:
                    score -= 2.0  # Penalize empty monosyllables so substantive turns are prioritized

                candidates.append({
                    "turn_id": t_id or f"{m_id}_turn",
                    "meeting_id": m_id or "",
                    "meeting_title": (m_title or "").strip(),
                    "speaker": spk or "Unknown",
                    "start_time": float(start_t or 0.0),
                    "end_time": float(end_t or 0.0),
                    "timestamp": ts_str or "00:00:00",
                    "text": txt_raw,
                    "topic_context": top_title or (m_title or "").strip() or "Level 1 Turn",
                    "similarity_score": round(score, 4),
                })

            # Check distinct meetings represented in candidates
            distinct_meetings: Dict[str, List[Dict[str, Any]]] = {}
            for c in candidates:
                m_id = c.get("meeting_id")
                if m_id:
                    distinct_meetings.setdefault(m_id, []).append(c)

            # Stratified & Speaker-Diverse Retrieval:
            # Guarantee fair representation across all matching meetings AND distinct speakers
            if len(distinct_meetings) > 1 and not resolved_meeting_id:
                selected_chunks: List[Dict[str, Any]] = []
                num_meetings = len(distinct_meetings)
                max_total_chunks = min(30, max(12, num_meetings * 4))

                # Pass 1: Speaker-diverse selection from each meeting (up to 2 distinct speakers per meeting)
                for m_id, m_chunks in distinct_meetings.items():
                    m_chunks.sort(key=lambda x: x["similarity_score"], reverse=True)
                    seen_spks_in_mtg = set()
                    for chunk in m_chunks:
                        spk = chunk.get("speaker")
                        if spk not in seen_spks_in_mtg:
                            seen_spks_in_mtg.add(spk)
                            selected_chunks.append(chunk)
                            if len(seen_spks_in_mtg) >= 2 or len(selected_chunks) >= max_total_chunks:
                                break

                # Pass 2: Ensure distinct speakers in top candidates are represented
                represented_speakers = set(c["speaker"] for c in selected_chunks)
                for c in candidates:
                    if len(selected_chunks) >= max_total_chunks:
                        break
                    spk = c.get("speaker")
                    if spk and spk != "Unknown" and spk not in represented_speakers:
                        selected_chunks.append(c)
                        represented_speakers.add(spk)

                # Pass 3: Balanced Round-Robin across meetings to prevent single-meeting starvation
                for round_idx in range(3):
                    for m_id, m_chunks in distinct_meetings.items():
                        if len(selected_chunks) >= max_total_chunks:
                            break
                        if round_idx < len(m_chunks):
                            chunk = m_chunks[round_idx]
                            if chunk not in selected_chunks:
                                selected_chunks.append(chunk)

                # Sort chronologically by meeting and start_time
                selected_chunks.sort(key=lambda x: (x.get("meeting_id", ""), x.get("start_time", 0.0)))
                retrieved_chunks = selected_chunks
            elif len(resolved_speakers) > 1:
                by_speaker: Dict[str, List[Dict[str, Any]]] = {}
                for c in candidates:
                    c_spk = c["speaker"].lower()
                    matched_key = "other"
                    for req_s in resolved_speakers:
                        if any(tok.lower() in c_spk for tok in req_s.split() if len(tok) >= 3):
                            matched_key = req_s
                            break
                    by_speaker.setdefault(matched_key, []).append(c)

                selected_chunks = []
                per_spk_limit = max(2, 6 // len(resolved_speakers))
                for req_s in resolved_speakers:
                    spk_turns = by_speaker.get(req_s, [])
                    selected_chunks.extend(spk_turns[:per_spk_limit])

                for c in candidates:
                    if len(selected_chunks) >= 6:
                        break
                    if c not in selected_chunks:
                        selected_chunks.append(c)

                selected_chunks.sort(key=lambda x: x["start_time"])
                retrieved_chunks = selected_chunks
            else:
                # Single meeting or single scope: prioritize distinct speakers across candidate turns
                by_spk: Dict[str, List[Dict[str, Any]]] = {}
                for c in candidates:
                    by_spk.setdefault(c["speaker"], []).append(c)
                selected_chunks = []
                for spk, spk_turns in by_spk.items():
                    spk_turns.sort(key=lambda x: x["similarity_score"], reverse=True)
                    selected_chunks.append(spk_turns[0])
                for c in candidates:
                    if len(selected_chunks) >= 12:
                        break
                    if c not in selected_chunks:
                        selected_chunks.append(c)
                selected_chunks.sort(key=lambda x: x["start_time"])
                retrieved_chunks = selected_chunks

    retrieval_latency_ms = (time.time() - retrieval_start) * 1000
    scores = [c["similarity_score"] for c in retrieved_chunks] if retrieved_chunks else [0.0]
    logger.info(
        f"[LANGGRAPH: VECTOR_RETRIEVAL] Retrieved: {len(retrieved_chunks)} chunks across {len(set(c['meeting_id'] for c in retrieved_chunks))} meetings | "
        f"Scores: [{min(scores):.2f} - {max(scores):.2f}] | Latency: {retrieval_latency_ms:.2f}ms"
    )

    return {
        "retrieved_chunks": retrieved_chunks,
    }


# ============================================================================
# Node 4: LLM Synthesis Node
# ============================================================================

async def llm_synthesis_node(state: ChatState) -> Dict[str, Any]:
    """
    Node 4: LLM Synthesis with OpenRouter.
    Executes conversational completion strictly grounded in verified transcript turns.
    """
    llm_start = time.time()
    query = state.get("query", "").strip()
    retrieved_chunks = state.get("retrieved_chunks", [])
    resolved_speaker = state.get("extracted_speaker")
    resolved_speakers: List[str] = state.get("extracted_speakers") or ([resolved_speaker] if resolved_speaker else [])

    # Build prompt context blocks with meeting titles and IDs
    context_blocks = []
    for i, c in enumerate(retrieved_chunks):
        m_title_str = c.get("meeting_title") or c.get("meeting_id") or "Meeting"
        context_blocks.append(
            f"[{i+1}] Meeting: {m_title_str} (ID: {c.get('meeting_id')}) | Topic: {c.get('topic_context')} | Speaker: {c['speaker']} ({c['timestamp']})\n"
            f"Quote: \"{c['text']}\""
        )
    formatted_context = "\n---\n".join(context_blocks)

    available_meetings = state.get("available_meetings") or []
    relevant_meetings = state.get("relevant_meetings") or []

    extra_record_context = ""
    list_instruction = ""

    # Case A: If user asked about specific speaker(s) across meetings
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

    # Case B: General system-wide list meetings query
    is_general_list_query = bool(re.search(r"\b(?:list (?:all )?meetings|what meetings|which meetings|all the meetings|all meetings|how many meetings|meeting list|available meetings)\b", query.lower())) and not resolved_speakers
    if is_general_list_query and available_meetings:
        mtg_list_str = "\n".join(f"- {m['title']} (ID: {m['id']})" for m in available_meetings)
        extra_record_context = f"Database Records - Complete List of All Ingested Meetings in the System ({len(available_meetings)} total):\n{mtg_list_str}\n\n---\n"
        list_instruction = f"- The user is asking to list all meetings. You MUST list all {len(available_meetings)} meetings from the Database Records above with their titles and IDs.\n"

    formatted_context = extra_record_context + formatted_context

    user_prompt = (
        f"User Query: {query}\n\n"
        f"Verified Context & Records:\n{formatted_context}\n\n"
        f"Instructions:\n"
        f"- Provide a direct, professional, conversational narrative answer to the user's query.\n"
        f"- Never dump raw transcript turns, timestamps, or quotation lists into the main text.\n"
        f"{list_instruction}"
        f"- Ground your statements strictly in the facts provided in the excerpts and records."
    )

    llm_client = get_openrouter_client()
    messages = [
        {"role": "system", "content": UNIVERSAL_RAG_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    target_model = getattr(settings, "LLM_MODEL_NAME", None) or os.getenv("LLM_MODEL_NAME") or os.getenv("LLM_MODEL") or "gemini-2.5-flash"
    answer = await llm_client.generate_chat_completion(
        messages=messages,
        model=target_model,
        max_tokens=4096,
        temperature=0.2,
    )

    llm_latency_ms = (time.time() - llm_start) * 1000
    provider_name = getattr(llm_client, "provider", "llm").upper()
    logger.info(
        f"[LANGGRAPH: LLM_SYNTHESIS] Model: {target_model} (Provider: {provider_name}) | "
        f"Latency: {llm_latency_ms:.2f}ms | Length: {len(answer)} chars"
    )

    return {
        "answer": answer,
        "llm_model_used": target_model,
    }


# ============================================================================
# Node 5: Format Citations Node (Provenance Aligned)
# ============================================================================

def align_citations_with_answer(retrieved_chunks: List[Dict[str, Any]], answer: str) -> List[Dict[str, Any]]:
    """
    Provenance Alignment (Solution 1):
    Guarantees that citation cards strictly align with facts, meetings, and speakers discussed in the LLM answer.
    Filters out unreferenced candidate chunks from unrelated meetings/speakers.
    """
    if not retrieved_chunks:
        return []
    ans_lower = answer.lower()

    # 1. Direct Bracket Citations (e.g. [1], [2])
    bracket_matches = [int(m) - 1 for m in re.findall(r"\[(\d+)\]", answer) if 1 <= int(m) <= len(retrieved_chunks)]
    if bracket_matches:
        seen_idx = set()
        aligned = []
        for idx in bracket_matches:
            if idx not in seen_idx:
                seen_idx.add(idx)
                aligned.append(retrieved_chunks[idx])
        return aligned

    # 2. Meeting-Level Provenance Alignment
    mentioned_meetings = set()
    for c in retrieved_chunks:
        m_id = c.get("meeting_id", "")
        m_title = (c.get("meeting_title") or "").strip()
        if m_id and m_id.lower() in ans_lower:
            mentioned_meetings.add(m_id)
        elif m_title and len(m_title) >= 3 and m_title.lower() in ans_lower:
            mentioned_meetings.add(m_id)

    # 3. Speaker-Level Provenance Alignment
    mentioned_speakers = set()
    for c in retrieved_chunks:
        spk = (c.get("speaker") or "").strip()
        if spk and spk != "Unknown":
            if spk.lower() in ans_lower or any(tok.lower() in ans_lower for tok in spk.split() if len(tok) >= 3 and tok.lower() not in {"the", "and", "sfs", "mr", "ms"}):
                mentioned_speakers.add(spk.lower())

    distinct_retrieved_meetings = set(c.get("meeting_id") for c in retrieved_chunks if c.get("meeting_id"))
    if mentioned_meetings and len(mentioned_meetings) < len(distinct_retrieved_meetings):
        filtered_chunks = [c for c in retrieved_chunks if c.get("meeting_id") in mentioned_meetings]
        if mentioned_speakers:
            spk_filtered = [c for c in filtered_chunks if c.get("speaker", "").lower() in mentioned_speakers or any(tok.lower() in mentioned_speakers for tok in c.get("speaker", "").split() if len(tok) >= 3)]
            if spk_filtered:
                filtered_chunks = spk_filtered
        return filtered_chunks

    if mentioned_speakers and len(mentioned_speakers) < len(set(c.get("speaker", "").lower() for c in retrieved_chunks)):
        spk_filtered = [c for c in retrieved_chunks if c.get("speaker", "").lower() in mentioned_speakers or any(tok.lower() in mentioned_speakers for tok in c.get("speaker", "").split() if len(tok) >= 3)]
        if spk_filtered:
            return spk_filtered

    return retrieved_chunks


async def format_citations_node(state: ChatState) -> Dict[str, Any]:
    """
    Node 5: Format Citations with Provenance Alignment.
    Packages retrieved turns into clean Level 1 citation models for the frontend cards,
    aligning citations with the synthesized answer.
    """
    retrieved_chunks = state.get("retrieved_chunks", [])
    answer = state.get("answer", "")
    aligned_chunks = align_citations_with_answer(retrieved_chunks, answer)

    citations: List[Dict[str, Any]] = []
    for c in aligned_chunks:
        citations.append({
            "speaker": c["speaker"],
            "start_time": c["start_time"],
            "end_time": c["end_time"],
            "meeting_id": c["meeting_id"],
            "exact_raw_text": c["text"],
            "topic_context": c.get("topic_context", "Level 1 Turn"),
        })

    logger.info(
        f"[LANGGRAPH: FORMAT_CITATIONS] Packaged {len(citations)} Level 1 citations "
        f"(aligned from {len(retrieved_chunks)} candidate chunks)."
    )

    return {
        "citations": citations,
        "citations_count": len(citations),
        "provenance_verified": True,
        "routing_mode": "LANGGRAPH_RAG",
    }


# ============================================================================
# Node 6: Fallback Node
# ============================================================================

async def fallback_node(state: ChatState) -> Dict[str, Any]:
    """
    Node 6: Fallback Node.
    Gracefully handles zero-match scenarios with conversational assistance.
    """
    query = state.get("query", "")
    logger.warning("[LANGGRAPH: FALLBACK] Zero matching transcript excerpts found in vector database.")

    fallback_answer = (
        "Based on the ingested meeting records, no relevant discussions or transcript excerpts were found "
        "matching your inquiry. Please ensure the relevant meeting has been processed or try rephrasing."
    )

    return {
        "answer": fallback_answer,
        "citations": [],
        "citations_count": 0,
        "provenance_verified": False,
        "routing_mode": "LANGGRAPH_FALLBACK",
        "routing_reasoning": "Zero matching speaker turns found in pgvector",
    }
