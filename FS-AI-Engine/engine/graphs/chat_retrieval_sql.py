"""
FS-AI-Engine/graphs/chat_retrieval_sql.py
=========================================
SQL building for vector_retrieval_node. With pgvector >= 0.8 the candidates come from two index-backed queries
(HNSW nearest neighbours + GIN full-text matches) that are merged; otherwise the single legacy query is used.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text

from ..core.config import get_settings

logger = logging.getLogger("fs-ai-engine.chat_graph")

# Placeholder inside the SELECT list that is replaced by the vector_sim expression of each query variant
VEC_SIM_TOKEN = "{vec_sim}"

_COSINE_SIM = "(1 - (t.embedding <=> CAST(:q_vec AS vector)))"
_DISTANCE = "t.embedding <=> CAST(:q_vec AS vector)"
_CASE_SIM = f"CASE WHEN t.embedding IS NOT NULL THEN {_COSINE_SIM} ELSE 0.0 END as vector_sim"
_TS_QUERY = "websearch_to_tsquery('english', :kw)"
_MIN_PGVECTOR = (0, 8, 0)

_version_checked = False
_iterative_supported = False


def _render(parts: list[str], sim_sql: str) -> str:
    return " ".join(parts).replace(VEC_SIM_TOKEN, f"{sim_sql} ")


def legacy_sql(parts: list[str], has_vector: bool, candidate_limit: int) -> str:
    """Today's single query: ordered by the computed similarity (cannot use the HNSW index)."""
    sql = _render(parts, _CASE_SIM if has_vector else "0.0 as vector_sim")
    if not has_vector:
        return sql + " ORDER BY t.start_time ASC"
    sql += " ORDER BY vector_sim DESC, t.start_time ASC"
    return sql + (f" LIMIT {int(candidate_limit)}" if candidate_limit else "")


def vector_sql(parts: list[str]) -> str:
    """Nearest neighbours by `<=>` so the HNSW index is used; the outer ORDER BY undoes relaxed_order drift."""
    inner = _render(parts, f"{_COSINE_SIM} as vector_sim, {_DISTANCE} AS distance")
    return f"SELECT * FROM ({inner} AND t.embedding IS NOT NULL ORDER BY {_DISTANCE} LIMIT :k) c ORDER BY c.distance"


def keyword_sql(parts: list[str]) -> str:
    """Full-text matches on the GIN-indexed tsv column, best ts_rank first."""
    return (
        _render(parts, _CASE_SIM)
        + f" AND t.tsv @@ {_TS_QUERY} ORDER BY ts_rank(t.tsv, {_TS_QUERY}) DESC LIMIT :k"
    )


def set_local_statements() -> list[str]:
    """SET LOCAL cannot take bind parameters, so the three validated ints are formatted in."""
    s = get_settings()
    return [
        "SET LOCAL hnsw.iterative_scan = 'relaxed_order'",
        f"SET LOCAL hnsw.ef_search = {int(s.hnsw_ef_search)}",
        f"SET LOCAL hnsw.max_scan_tuples = {int(s.hnsw_max_scan_tuples)}",
    ]


def parse_version(raw: Any) -> tuple[int, ...] | None:
    """'0.8.1' -> (0, 8, 1); None when it cannot be read."""
    try:
        return tuple(int(p) for p in str(raw).split(".")[:3])
    except (TypeError, ValueError):
        return None


def version_supports_iterative(raw: Any) -> bool:
    parsed = parse_version(raw)
    return parsed is not None and parsed >= _MIN_PGVECTOR


async def pgvector_supports_iterative(session: Any) -> bool:
    """Reads the pgvector version once per process; logs one ERROR when iterative scans are unavailable."""
    global _version_checked, _iterative_supported
    if not _version_checked:
        try:
            version = (await session.execute(text("SELECT extversion FROM pg_extension WHERE extname='vector'"))).scalar()
        except Exception as exc:
            version = None
            logger.warning(f"[LANGGRAPH: VECTOR_RETRIEVAL] Could not read pgvector version: {exc}")
        _iterative_supported = version_supports_iterative(version)
        _version_checked = True
        if not _iterative_supported:
            logger.error(
                f"[LANGGRAPH: VECTOR_RETRIEVAL] pgvector {version or 'missing'} is below 0.8.0 (no iterative index scans): "
                "using the unindexed retrieval query."
            )
    return _iterative_supported


def merge_rows(vector_rows: list[Any], keyword_rows: list[Any]) -> list[Any]:
    """Union by turn_id (vector row wins), ordered like the legacy query: similarity desc, start_time asc."""
    merged: dict[Any, Any] = {r[0]: r for r in keyword_rows}
    merged.update({r[0]: r for r in vector_rows})
    return sorted(merged.values(), key=lambda r: (-float(r[10] or 0.0), float(r[4] or 0.0)))


async def fetch_candidates(
    session: Any, parts: list[str], params: dict[str, Any], keyword_text: str, candidate_limit: int
) -> list[Any]:
    """Runs the HNSW and GIN candidate queries in the session's transaction and merges them."""
    for stmt in set_local_statements():
        await session.execute(text(stmt))
    bound = {**params, "k": int(candidate_limit)}
    vector_rows = (await session.execute(text(vector_sql(parts)), bound)).fetchall()
    keyword_rows: list[Any] = []
    if keyword_text.strip():
        keyword_rows = (await session.execute(text(keyword_sql(parts)), {**bound, "kw": keyword_text})).fetchall()
    return merge_rows(vector_rows, keyword_rows)
