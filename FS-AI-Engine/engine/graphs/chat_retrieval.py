"""
FS-AI-Engine/graphs/chat_retrieval.py
=====================================
Retrieval nodes of the chat pipeline:
- embed_query_node: Generates 1536-dimensional query vector embeddings.
- vector_retrieval_node: Executes PostgreSQL pgvector cosine similarity search with metadata filtering.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from sqlalchemy import text

from ..core.database import AsyncSessionLocal
from ..services.embedding_service import EmbeddingService
from .chat_query import MEETING_STOP_WORDS, ChatState, _speaker_clause, _state_speakers

# Dedicated AI Service Logger
logger = logging.getLogger("fs-ai-engine.chat_graph")


def enrich_chunk_text(speaker: str, timestamp: str, text: str) -> str:
    """Prefixes a transcript chunk with speaker and timestamp metadata for embedding/retrieval context."""
    clean_speaker = (speaker or "Unknown").strip()
    clean_timestamp = (timestamp or "00:00:00").strip()
    clean_text = (text or "").strip()
    return f"Speaker: {clean_speaker} | Timestamp: {clean_timestamp} | Content: {clean_text}"


def _name_tokens_in(speaker_lower: str, target: str) -> bool:
    """True if any 3+ letter token of `target` occurs inside the lowercased speaker name."""
    return any(tok.lower() in speaker_lower for tok in target.split() if len(tok) >= 3)


def _top_up(selected: list[dict[str, Any]], candidates: list[dict[str, Any]], limit: int) -> None:
    """Appends not-yet-selected candidates, in order, until `limit` chunks are selected."""
    for c in candidates:
        if len(selected) >= limit:
            break
        if c not in selected:
            selected.append(c)


# ============================================================================
# Node 2: Embed Query Node
# ============================================================================

async def embed_query_node(state: ChatState) -> dict[str, Any]:
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

async def vector_retrieval_node(state: ChatState) -> dict[str, Any]:
    """
    Node 3: Vector Retrieval with pgvector & Metadata Filtering.
    Queries PostgreSQL speaker_turns with cosine distance (1 - <=> vector)
    and applies metadata filters on speaker, meeting, and project JSONB columns.
    """
    retrieval_start = time.time()
    query = state.get("query", "").strip()
    query_lower = query.lower()
    query_vector = state.get("query_embedding", [])
    resolved_speakers: list[str] = _state_speakers(state)

    user_id = state["user_id"]  # required: every query is scoped to one account
    resolved_meeting_id = state.get("meeting_id")
    project_id = state.get("project_id")

    tokens = re.findall(r"[a-zA-Z0-9_\-]+", query_lower)
    meaningful_keywords = [w for w in tokens if len(w) > 2 and w not in MEETING_STOP_WORDS]
    query_tokens_set = set(meaningful_keywords)

    has_vector = bool(query_vector and len(query_vector) == 1536)
    vec_str = "[" + ",".join(str(x) for x in query_vector) + "]" if has_vector else "[]"

    retrieved_chunks: list[dict[str, Any]] = []

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
                "WHERE t.user_id = :u_id"
            ]
            params: dict[str, Any] = {
                "has_vector": has_vector,
                "q_vec": vec_str,
                "u_id": user_id,
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
                    spk_parts = [p.strip() for p in re.findall(r"\w+", spk_target) if len(p.strip()) >= 3 and p.lower() not in MEETING_STOP_WORDS]
                    p_full = f"spk_full_{s_idx}"
                    params[p_full] = f"%{spk_target.lower()}%"
                    sub_clauses = [_speaker_clause(p_full)]
                    for p_idx, part in enumerate(spk_parts):
                        p_name = f"spk_token_{s_idx}_{p_idx}"
                        sub_clauses.append(_speaker_clause(p_name))
                        params[p_name] = f"%{part.lower()}%"
                    all_spk_clauses.append(f"({' OR '.join(sub_clauses)})")
                query_parts.append(f"AND ({' OR '.join(all_spk_clauses)})")

            order_by = " ORDER BY vector_sim DESC, t.start_time ASC" if has_vector else " ORDER BY t.start_time ASC"
            query_sql = " ".join(query_parts) + order_by
            res = await session.execute(text(query_sql), params)
            rows = res.fetchall()

            # Resilient Fallback: If strict speaker filter yielded 0 rows, search without speaker filter
            if not rows and resolved_speakers:
                logger.info(f"[LANGGRAPH: VECTOR_RETRIEVAL] Fallback search across all participants for '{resolved_speakers}'.")
                fallback_parts = [p for p in query_parts if not p.startswith("AND (t.metadata->>'speaker'") and not p.startswith("AND ((t.metadata->>'speaker'") and not p.startswith("AND (((t.metadata->>'speaker'")]
                fallback_sql = " ".join(fallback_parts) + order_by
                fallback_params = {k: v for k, v in params.items() if not k.startswith("spk_")}
                res = await session.execute(text(fallback_sql), fallback_params)
                rows = res.fetchall()

            candidates: list[dict[str, Any]] = []
            for r in rows:
                t_id, m_id, spk, txt, start_t, end_t, ts_str, _meta_json, m_title, top_title, vec_sim = (
                    r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10]
                )
                txt_raw = (txt or "").strip()
                # Downweight non-substantive conversational fillers (< 15 chars monosyllables)
                is_filler = len(txt_raw) < 15 and bool(re.match(r"^(?:ah|ok|okay|yes|no|yeah|right|correct|hello|hi|bye|thanks)[\.\,\s]*$", txt_raw.lower()))

                enriched_chunk = enrich_chunk_text(spk, ts_str, txt_raw)
                enriched_lower = enriched_chunk.lower()

                vector_score = max(0.0, float(vec_sim or 0.0))
                kw_matches = sum(1 for kw in query_tokens_set if kw in enriched_lower)
                score = (vector_score * 3.0) + (float(kw_matches) * 1.5)

                if resolved_speakers and any(_name_tokens_in((spk or "").lower(), s) for s in resolved_speakers):
                    score += 3.5

                if is_filler:
                    score -= 2.0

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
            distinct_meetings: dict[str, list[dict[str, Any]]] = {}
            for c in candidates:
                m_id = c.get("meeting_id")
                if m_id:
                    distinct_meetings.setdefault(m_id, []).append(c)

            # Stratified & Speaker-Diverse Retrieval
            if len(distinct_meetings) > 1 and not resolved_meeting_id:
                selected_chunks: list[dict[str, Any]] = []
                num_meetings = len(distinct_meetings)
                max_total_chunks = min(30, max(12, num_meetings * 4))

                # Pass 1: Speaker-diverse selection from each meeting
                for m_chunks in distinct_meetings.values():
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
                represented_speakers = {c["speaker"] for c in selected_chunks}
                for c in candidates:
                    if len(selected_chunks) >= max_total_chunks:
                        break
                    spk = c.get("speaker")
                    if spk and spk != "Unknown" and spk not in represented_speakers:
                        selected_chunks.append(c)
                        represented_speakers.add(spk)

                # Pass 3: Balanced Round-Robin across meetings
                for round_idx in range(3):
                    for m_chunks in distinct_meetings.values():
                        if len(selected_chunks) >= max_total_chunks:
                            break
                        if round_idx < len(m_chunks):
                            chunk = m_chunks[round_idx]
                            if chunk not in selected_chunks:
                                selected_chunks.append(chunk)

                selected_chunks.sort(key=lambda x: (x.get("meeting_id", ""), x.get("start_time", 0.0)))
                retrieved_chunks = selected_chunks
            elif len(resolved_speakers) > 1:
                by_speaker: dict[str, list[dict[str, Any]]] = {}
                for c in candidates:
                    c_spk = c["speaker"].lower()
                    matched_key = "other"
                    for req_s in resolved_speakers:
                        if _name_tokens_in(c_spk, req_s):
                            matched_key = req_s
                            break
                    by_speaker.setdefault(matched_key, []).append(c)

                selected_chunks = []
                per_spk_limit = max(2, 6 // len(resolved_speakers))
                for req_s in resolved_speakers:
                    spk_turns = by_speaker.get(req_s, [])
                    selected_chunks.extend(spk_turns[:per_spk_limit])

                _top_up(selected_chunks, candidates, 6)

                selected_chunks.sort(key=lambda x: x["start_time"])
                retrieved_chunks = selected_chunks
            else:
                by_spk: dict[str, list[dict[str, Any]]] = {}
                for c in candidates:
                    by_spk.setdefault(c["speaker"], []).append(c)
                selected_chunks = []
                for spk_turns in by_spk.values():
                    spk_turns.sort(key=lambda x: x["similarity_score"], reverse=True)
                    selected_chunks.append(spk_turns[0])
                _top_up(selected_chunks, candidates, 12)
                selected_chunks.sort(key=lambda x: x["start_time"])
                retrieved_chunks = selected_chunks

    retrieval_latency_ms = (time.time() - retrieval_start) * 1000
    scores = [c["similarity_score"] for c in retrieved_chunks] if retrieved_chunks else [0.0]
    logger.info(
        f"[LANGGRAPH: VECTOR_RETRIEVAL] Retrieved: {len(retrieved_chunks)} chunks across {len({c['meeting_id'] for c in retrieved_chunks})} meetings | "
        f"Scores: [{min(scores):.2f} - {max(scores):.2f}] | Latency: {retrieval_latency_ms:.2f}ms"
    )

    return {
        "retrieved_chunks": retrieved_chunks,
    }
