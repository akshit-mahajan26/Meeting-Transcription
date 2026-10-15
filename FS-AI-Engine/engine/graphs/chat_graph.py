"""
FS-AI-Engine/graphs/chat_graph.py
=================================
Compiled LangGraph StateGraph Orchestration for the Meeting Chatbot.

Defines the graph topology, conditional branching edges, and asynchronous
execution entrypoint for FastAPI engine routes.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from langgraph.graph import END, START, StateGraph

from ..core.config import settings
from .chat_nodes import (
    ChatState,
    embed_query_node,
    extract_filters_node,
    fallback_node,
    format_citations_node,
    llm_synthesis_node,
    vector_retrieval_node,
)

logger = logging.getLogger("fs-ai-engine.chat_graph")


# ============================================================================
# Routing Conditions
# ============================================================================

def route_after_filters(state: ChatState) -> str:
    """
    Catalog questions (counts, lists of projects/meetings/participants) are answered from the
    relational records, so the embedding and vector search are skipped for them.
    """
    if state.get("no_meeting_on_date"):
        return "fallback"  # nothing was held on the requested date, so there is nothing to retrieve
    return "llm_synthesis" if state.get("skip_retrieval") else "embed_query"


def route_after_retrieval(state: ChatState) -> str:
    """
    Evaluates whether relevant vector matches or verified catalog/relational records exist.
    Routes to LLM synthesis if candidate turns, catalog records, or involvement records exist, or fallback otherwise.
    """
    if state.get("query_intent") == "catalog":
        return "llm_synthesis"
    if state.get("relevant_meetings"):
        return "llm_synthesis"
    chunks = state.get("retrieved_chunks", [])
    if chunks and len(chunks) > 0:
        return "llm_synthesis"
    return "fallback"


# ============================================================================
# Graph Topology Construction & Compilation
# ============================================================================

def build_chat_graph() -> StateGraph:
    """Constructs and compiles the production LangGraph chat workflow."""
    workflow = StateGraph(ChatState)

    # 1. Register Graph Nodes
    workflow.add_node("extract_filters", extract_filters_node)
    workflow.add_node("embed_query", embed_query_node)
    workflow.add_node("vector_retrieval", vector_retrieval_node)
    workflow.add_node("llm_synthesis", llm_synthesis_node)
    workflow.add_node("format_citations", format_citations_node)
    workflow.add_node("fallback", fallback_node)

    # 2. Linear Flow
    workflow.add_edge(START, "extract_filters")
    workflow.add_conditional_edges(
        "extract_filters",
        route_after_filters,
        {"embed_query": "embed_query", "llm_synthesis": "llm_synthesis", "fallback": "fallback"},
    )
    workflow.add_edge("embed_query", "vector_retrieval")

    # 3. Conditional Branching based on Retrieval Quality
    workflow.add_conditional_edges(
        "vector_retrieval",
        route_after_retrieval,
        {
            "llm_synthesis": "llm_synthesis",
            "fallback": "fallback",
        },
    )

    # 4. Citation Packaging and Terminal Transitions
    workflow.add_edge("llm_synthesis", "format_citations")
    workflow.add_edge("format_citations", END)
    workflow.add_edge("fallback", END)

    return workflow.compile()


# Singleton compiled graph instance
compiled_chat_graph = build_chat_graph()


# ============================================================================
# Asynchronous Execution Entrypoint
# ============================================================================

async def run_chat_graph(
    query: str,
    user_id: str,
    project_id: str | None = None,
    meeting_id: str | None = None,
    speaker: str | None = None,
    top_k: int = 5,
    history: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """
    Entrypoint invoked by FastAPI engine routers.
    Executes query through the compiled LangGraph StateGraph.
    """
    start_time = time.time()
    logger.info("=" * 70)
    logger.info(
        f"[LANGGRAPH CHAT GRAPH] Invoking query: '{query}' | "
        f"Project: {project_id} | Meeting: {meeting_id} | Speaker: {speaker}"
    )

    initial_state: ChatState = {
        "query": query.strip(),
        "user_id": user_id,
        "project_id": project_id,
        "meeting_id": meeting_id,
        "extracted_speaker": speaker,
        "history": history or [],
        "query_embedding": [],
        "retrieved_chunks": [],
        "answer": "",
        "citations": [],
        "citations_count": 0,
        "provenance_verified": True,
        "reasoning": "",
        "routing_mode": "LANGGRAPH_RAG",
        "llm_model_used": getattr(settings, "LLM_MODEL_NAME", "gemini-2.5-flash"),
        "start_time": start_time,
    }

    final_state = await compiled_chat_graph.ainvoke(initial_state)

    total_latency_ms = (time.time() - start_time) * 1000
    final_state["latency_ms"] = total_latency_ms

    logger.info(
        f"[LANGGRAPH CHAT GRAPH COMPLETE] Total Latency: {total_latency_ms:.2f}ms | "
        f"Mode: {final_state.get('routing_mode')} | Citations: {final_state.get('citations_count')}"
    )
    logger.info("=" * 70)

    return final_state
