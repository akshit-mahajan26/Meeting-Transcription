"""
FS-AI-Engine/graphs/chat_kg.py
==============================
Knowledge graph retrieval nodes of the chat pipeline (they run inside the "rag" subgraph):
- kg_retrieval_node: finds the graph entities the question is about (query embedding + resolved participants),
  expands their relations one hop and writes them as `graph_facts`, alongside vector retrieval.
- graph_evidence_node: after both retrievals, adds the transcript turns that ground those facts to the
  excerpts when vector search did not already return them, so every graph fact can be cited.

Both are no-ops when KG_DATABASE_URL is not configured or the graph is empty.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy import text

from ..core.database import AsyncSessionLocal
from ..core.kg_database import KGAsyncSessionLocal
from .chat_query import ChatState, _state_speakers

logger = logging.getLogger("fs-ai-engine.chat_graph")

SEED_MIN_SIMILARITY = 0.6  # cosine similarity between the question and an entity for it to seed the expansion
MAX_SEEDS = 6
MAX_FACTS = 30
MAX_GRAPH_EVIDENCE = 8  # turns added to the excerpts on top of the vector results
SKELETON_RELATIONS = ("participated_in", "part_of", "discussed")  # structural edges, ranked below extracted ones


def _explicit_meeting(state: ChatState) -> str | None:
    """The meeting the user picked (a guessed scope does not narrow the graph)."""
    return None if state.get("meeting_scope_inferred") else state.get("meeting_id")


async def kg_retrieval_node(state: ChatState) -> dict[str, Any]:
    if KGAsyncSessionLocal is None:
        return {"graph_facts": [], "graph_turn_ids": []}

    started = time.time()
    user_id = state["user_id"]
    project_id = state.get("project_id")
    meeting_id = _explicit_meeting(state)
    query_vector = state.get("query_embedding") or []
    speakers = _state_speakers(state)

    scope_sql = ""
    params: dict[str, Any] = {"u_id": user_id}
    if project_id or meeting_id:
        conditions = ["m.entity_id = e.id", "m.user_id = :u_id"]
        if project_id:
            conditions.append("m.project_id = :p_id")
            params["p_id"] = project_id
        if meeting_id:
            conditions.append("m.meeting_id = :m_id")
            params["m_id"] = meeting_id
        scope_sql = f" AND EXISTS (SELECT 1 FROM kg_mentions m WHERE {' AND '.join(conditions)})"

    try:
        async with KGAsyncSessionLocal() as session:
            seeds: dict[str, float] = {}
            if len(query_vector) == 1536:
                rows = (await session.execute(
                    text(
                        "SELECT e.id, 1 - (e.embedding <=> CAST(:q_vec AS vector)) AS sim FROM kg_entities e "
                        "WHERE e.user_id = :u_id AND e.embedding IS NOT NULL AND e.entity_type <> 'meeting'"
                        f"{scope_sql} ORDER BY e.embedding <=> CAST(:q_vec AS vector) LIMIT :lim"
                    ),
                    {**params, "q_vec": "[" + ",".join(str(x) for x in query_vector) + "]", "lim": MAX_SEEDS},
                )).fetchall()
                seeds.update({r[0]: float(r[1]) for r in rows if float(r[1]) >= SEED_MIN_SIMILARITY})

            for idx, speaker in enumerate(speakers):
                params[f"spk_{idx}"] = f"%{speaker.strip().lower()}%"
                rows = (await session.execute(
                    text(
                        f"SELECT e.id FROM kg_entities e WHERE e.user_id = :u_id AND e.entity_type = 'person' "
                        f"AND e.normalized_name LIKE :spk_{idx}{scope_sql} LIMIT 2"
                    ),
                    params,
                )).fetchall()
                seeds.update({r[0]: 1.0 for r in rows})

            if not seeds:
                return {"graph_facts": [], "graph_turn_ids": []}

            rel_scope = ""
            if project_id:
                rel_scope += " AND r.project_id = :p_id"
            if meeting_id:
                rel_scope += " AND r.meeting_id = :m_id"
            rows = (await session.execute(
                text(
                    "SELECT s.id, s.name, s.entity_type, t.id, t.name, t.entity_type, r.relation_type, r.description, "
                    "r.meeting_id, r.turn_ids, r.confidence "
                    "FROM kg_relations r JOIN kg_entities s ON s.id = r.source_id JOIN kg_entities t ON t.id = r.target_id "
                    f"WHERE r.user_id = :u_id AND (r.source_id = ANY(:seeds) OR r.target_id = ANY(:seeds)){rel_scope}"
                ),
                {**params, "seeds": list(seeds)},
            )).fetchall()
    except Exception as exc:  # the graph is an add-on: never fail the answer because of it
        logger.warning(f"[LANGGRAPH: KG_RETRIEVAL] Graph lookup failed, continuing without it: {exc}")
        return {"graph_facts": [], "graph_turn_ids": []}

    def rank(r) -> tuple:
        seed_score = max(seeds.get(r[0], 0.0), seeds.get(r[3], 0.0))
        return (r[6] in SKELETON_RELATIONS, -seed_score, -float(r[10] or 0.0))

    titles = {m["id"]: (m.get("title"), m.get("date")) for m in state.get("available_meetings") or []}
    facts: list[str] = []
    turn_ids: list[str] = []
    seen: set[tuple] = set()
    for r in sorted(rows, key=rank):
        key = (r[0], r[3], r[6], r[8])
        if key in seen:
            continue
        seen.add(key)
        title, date = titles.get(r[8], (None, None))
        if title is None and r[8] not in titles and state.get("available_meetings"):
            continue  # a meeting outside the current scope (or no longer in the library)
        where = f"{title or r[8]}{f', {date}' if date else ''}"
        fact = f"- {r[1]} ({r[2].replace('_', ' ')}) --{r[6]}--> {r[4]} ({r[5].replace('_', ' ')}) [Meeting: {where}]"
        if r[7]:
            fact += f": {r[7]}"
        facts.append(fact)
        if r[9] and r[6] not in SKELETON_RELATIONS:
            turn_ids.append(r[9][0])
        if len(facts) >= MAX_FACTS:
            break

    logger.info(
        f"[LANGGRAPH: KG_RETRIEVAL] Seeds: {len(seeds)} | Facts: {len(facts)} | Evidence turns: {len(turn_ids)} | "
        f"Latency: {(time.time() - started) * 1000:.2f}ms"
    )
    return {"graph_facts": facts, "graph_turn_ids": list(dict.fromkeys(turn_ids))}


async def graph_evidence_node(state: ChatState) -> dict[str, Any]:
    """Appends the graph's grounding turns that vector retrieval did not return (same chunk shape)."""
    chunks = list(state.get("retrieved_chunks") or [])
    have = {c.get("turn_id") for c in chunks}
    missing = [t for t in state.get("graph_turn_ids") or [] if t not in have][:MAX_GRAPH_EVIDENCE]
    if not missing or AsyncSessionLocal is None:
        return {}

    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            text(
                "SELECT t.turn_id, t.meeting_id, COALESCE(t.metadata->>'speaker', t.speaker_name, 'Unknown'), t.text, "
                "t.start_time, t.end_time, COALESCE(t.metadata->>'timestamp', to_char(to_timestamp(t.start_time), 'HH24:MI:SS')), "
                "m.title, top.title "
                "FROM speaker_turns t LEFT JOIN meetings m ON t.meeting_id = m.id "
                "LEFT JOIN meeting_topics top ON t.topic_id = top.id "
                "WHERE t.user_id = :u_id AND t.turn_id = ANY(:ids)"
            ),
            {"u_id": state["user_id"], "ids": missing},
        )).fetchall()

    for r in rows:
        chunks.append({
            "turn_id": r[0],
            "meeting_id": r[1] or "",
            "meeting_title": (r[7] or "").strip(),
            "speaker": r[2] or "Unknown",
            "start_time": float(r[4] or 0.0),
            "end_time": float(r[5] or 0.0),
            "timestamp": r[6] or "00:00:00",
            "text": (r[3] or "").strip(),
            "topic_context": r[8] or (r[7] or "").strip() or "Level 1 Turn",
            "similarity_score": 0.0,
        })
    logger.info(f"[LANGGRAPH: GRAPH_EVIDENCE] Added {len(rows)} graph-grounded turns to {len(have)} vector results.")
    return {"retrieved_chunks": chunks}
