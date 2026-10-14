"""
FS-AI-Engine/engine/services/rag_service.py
===========================================
Query service: runs a user question through the LangGraph chat pipeline
(`graphs/chat_graph.py`) and maps the result to the API response model.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from ..core.config import settings
from ..graphs.chat_graph import run_chat_graph

logger = logging.getLogger("fs-ai-engine.rag_service")


class CitationModel(BaseModel):
    speaker: str
    start_time: float
    end_time: float
    meeting_id: str
    exact_raw_text: str
    topic_context: str | None = "Level 1 Turn"


class RAGQueryResponse(BaseModel):
    query: str
    answer: str
    citations: list[CitationModel] = Field(default_factory=list)
    citations_count: int = 0
    provenance_verified: bool = True
    routing_mode: str = "MICRO"
    routing_confidence: float = 1.0
    routing_reasoning: str = ""
    llm_model_used: str = getattr(settings, "LLM_MODEL_NAME", "gemini-2.5-flash")


async def process_user_query(
    query: str,
    user_id: str,
    project_id: str | None = None,
    meeting_id: str | None = None,
    speaker: str | None = None,
    top_k: int = 5,
) -> RAGQueryResponse:
    """Answer a question with the chat graph. Errors propagate to the caller (no hidden second pipeline)."""
    clean_query = query.strip()
    logger.info(
        f"[QUERY] '{clean_query}' | Project: {project_id} | Meeting: {meeting_id} | Speaker: {speaker}"
    )

    graph_res = await run_chat_graph(
        query=clean_query,
        user_id=user_id,
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
