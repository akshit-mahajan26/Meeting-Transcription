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
from collections.abc import AsyncIterator
from typing import Any

from langgraph.graph import END, START, StateGraph

from ..core.config import settings
from .chat_nodes import ChatState, fallback_node
from .chat_query import QueryIntent
from .chat_subgraphs import (
    build_rag_subgraph,
    build_records_subgraph,
    build_understand_subgraph,
    route_after_retrieval,
)

__all__ = ["build_chat_graph", "compiled_chat_graph", "route_after_filters", "route_after_retrieval", "run_chat_graph", "smalltalk_node"]

logger = logging.getLogger("fs-ai-engine.chat_graph")


# ============================================================================
# Routing Conditions
# ============================================================================

_DEFAULT_SMALLTALK_REPLY = (
    "Hello! I can help with your meeting transcripts. Ask about discussions, decisions, action items, "
    "participants or projects."
)


async def smalltalk_node(state: ChatState) -> dict[str, Any]:
    """Greetings and pleasantries get the reply the rewrite step left in `answer`: no retrieval, no citations."""
    return {
        "answer": state.get("answer") or _DEFAULT_SMALLTALK_REPLY,
        "citations": [],
        "citations_count": 0,
        "provenance_verified": True,
        "routing_mode": "LANGGRAPH_SMALLTALK",
        "reasoning": "Greeting or pleasantry; no meeting lookup needed",
    }


def route_after_filters(state: ChatState) -> str:
    """
    Picks the answer pipeline. Catalog and attendance questions are answered from the relational records
    ("records"), so the embedding and vector search are skipped for them; everything else goes through "rag".
    """
    if state.get("query_intent") == QueryIntent.SMALLTALK:
        return "smalltalk"
    if state.get("no_meeting_on_date"):
        return "fallback"  # nothing was held on the requested date, so there is nothing to retrieve
    return "records" if state.get("skip_retrieval") else "rag"


# ============================================================================
# Graph Topology Construction & Compilation
# ============================================================================

def build_chat_graph() -> StateGraph:
    """Constructs and compiles the production LangGraph chat workflow."""
    workflow = StateGraph(ChatState)

    workflow.add_node("understand", build_understand_subgraph())
    workflow.add_node("smalltalk", smalltalk_node)
    workflow.add_node("fallback", fallback_node)
    workflow.add_node("records", build_records_subgraph())
    workflow.add_node("rag", build_rag_subgraph())

    workflow.add_edge(START, "understand")
    workflow.add_conditional_edges(
        "understand",
        route_after_filters,
        {"smalltalk": "smalltalk", "fallback": "fallback", "records": "records", "rag": "rag"},
    )
    for terminal in ("smalltalk", "fallback", "records", "rag"):
        workflow.add_edge(terminal, END)

    return workflow.compile()


# Singleton compiled graph instance
compiled_chat_graph = build_chat_graph()


# ============================================================================
# Asynchronous Execution Entrypoints
# ============================================================================

def _initial_state(
    query: str,
    user_id: str,
    project_id: str | None,
    meeting_id: str | None,
    speaker: str | None,
    history: list[dict[str, str]] | None,
) -> ChatState:
    start_time = time.time()
    logger.info("=" * 70)
    logger.info(
        f"[LANGGRAPH CHAT GRAPH] Invoking query: '{query}' | "
        f"Project: {project_id} | Meeting: {meeting_id} | Speaker: {speaker}"
    )
    return {
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


def _finish(final_state: dict[str, Any], start_time: float) -> dict[str, Any]:
    total_latency_ms = (time.time() - start_time) * 1000
    final_state["latency_ms"] = total_latency_ms

    logger.info(
        f"[LANGGRAPH CHAT GRAPH COMPLETE] Total Latency: {total_latency_ms:.2f}ms | "
        f"Mode: {final_state.get('routing_mode')} | Citations: {final_state.get('citations_count')}"
    )
    logger.info("=" * 70)
    return final_state


async def run_chat_graph(
    query: str,
    user_id: str,
    project_id: str | None = None,
    meeting_id: str | None = None,
    speaker: str | None = None,
    history: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """
    Entrypoint invoked by FastAPI engine routers.
    Executes query through the compiled LangGraph StateGraph.
    """
    initial_state = _initial_state(query, user_id, project_id, meeting_id, speaker, history)
    final_state = await compiled_chat_graph.ainvoke(initial_state)
    return _finish(final_state, initial_state["start_time"])


# What the reader is told while the pipeline works, by the route the question took
_PIPELINE_STATUS = {"records": "Looking up meeting records…", "rag": "Searching transcripts…"}


async def stream_chat_graph(
    query: str,
    user_id: str,
    project_id: str | None = None,
    meeting_id: str | None = None,
    speaker: str | None = None,
    history: list[dict[str, str]] | None = None,
) -> AsyncIterator[tuple[str, Any]]:
    """
    Runs the same graph as run_chat_graph, reporting progress as it goes. Yields:
    - ("status", text): what the pipeline is doing now (once per phase);
    - ("token", text): the next piece of the answer, as the LLM writes it (only questions answered by the LLM;
      small talk and "nothing found" replies arrive in the final state only);
    - ("final", state): the complete final state, identical to run_chat_graph's result. Its `answer` is the
      canonical text (SOURCES line and excerpt markers removed) and may differ slightly from the streamed text.
    """
    initial_state = _initial_state(query, user_id, project_id, meeting_id, speaker, history)
    yield ("status", "Understanding your question…")

    final_state: dict[str, Any] | None = None
    announced = False
    async for namespace, mode, payload in compiled_chat_graph.astream(
        initial_state,
        config={"configurable": {"stream_answer": True}},
        stream_mode=["custom", "values"],
        subgraphs=True,  # custom events from the nested subgraphs only reach the caller with this on
    ):
        if mode == "custom":
            if payload.get("type") == "token":
                yield ("token", payload["text"])
        elif not namespace:  # a state snapshot of the parent graph; the last one is the final state
            final_state = payload
            if not announced and "skip_retrieval" in payload:  # the understand phase has finished
                announced = True
                if message := _PIPELINE_STATUS.get(route_after_filters(payload)):
                    yield ("status", message)

    if final_state is None:
        raise RuntimeError("The chat graph finished without producing a state")
    yield ("final", _finish(final_state, initial_state["start_time"]))
