"""
FS-AI-Engine/graphs/chat_subgraphs.py
=====================================
Compiled LangGraph subgraphs of the Meeting Chatbot.

- understand: loads the account context and rewrites the query in parallel, then analyses it and resolves the
  speaker / meeting scope and the relational records. Replaces the former single extract_filters node.
- records: answered purely from relational records (catalog / attendance questions); no embedding or vector search.
- rag: embeds the query, searches transcript turns, then synthesises or falls back when nothing matched.

Both share ChatState with the parent graph, so each compiled subgraph is added to it directly as a node.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from .chat_nodes import (
    ChatState,
    analyze_query_node,
    embed_query_node,
    fallback_node,
    format_citations_node,
    llm_synthesis_node,
    load_context_node,
    lookup_records_node,
    resolve_scope_node,
    rewrite_query_node,
    vector_retrieval_node,
)
from .chat_query import QueryIntent


def route_after_retrieval(state: ChatState) -> str:
    """
    Evaluates whether relevant vector matches or verified catalog/relational records exist.
    Routes to LLM synthesis if candidate turns, catalog records, or involvement records exist, or fallback otherwise.
    """
    if state.get("query_intent") == QueryIntent.CATALOG:
        return "llm_synthesis"
    if state.get("relevant_meetings"):
        return "llm_synthesis"
    chunks = state.get("retrieved_chunks", [])
    if chunks and len(chunks) > 0:
        return "llm_synthesis"
    return "fallback"


def build_understand_subgraph():
    """Query understanding: (load_context || rewrite_query) -> analyze_query -> resolve_scope -> lookup_records."""
    graph = StateGraph(ChatState)
    graph.add_node("load_context", load_context_node)
    graph.add_node("rewrite_query", rewrite_query_node)
    graph.add_node("analyze_query", analyze_query_node)
    graph.add_node("resolve_scope", resolve_scope_node)
    graph.add_node("lookup_records", lookup_records_node)

    graph.add_edge(START, "load_context")
    graph.add_edge(START, "rewrite_query")
    graph.add_edge(["load_context", "rewrite_query"], "analyze_query")  # waits for both
    graph.add_edge("analyze_query", "resolve_scope")
    graph.add_edge("resolve_scope", "lookup_records")
    graph.add_edge("lookup_records", END)
    return graph.compile()


def build_records_subgraph():
    """Relational-records pipeline: synthesis over catalog/involvement records, then citation packaging."""
    graph = StateGraph(ChatState)
    graph.add_node("llm_synthesis", llm_synthesis_node)
    graph.add_node("format_citations", format_citations_node)
    graph.add_edge(START, "llm_synthesis")
    graph.add_edge("llm_synthesis", "format_citations")
    graph.add_edge("format_citations", END)
    return graph.compile()


def build_rag_subgraph():
    """Transcript RAG pipeline: embed, vector search, then synthesis with citations or a fallback reply."""
    graph = StateGraph(ChatState)
    graph.add_node("embed_query", embed_query_node)
    graph.add_node("vector_retrieval", vector_retrieval_node)
    graph.add_node("llm_synthesis", llm_synthesis_node)
    graph.add_node("format_citations", format_citations_node)
    graph.add_node("fallback", fallback_node)

    graph.add_edge(START, "embed_query")
    graph.add_edge("embed_query", "vector_retrieval")
    graph.add_conditional_edges(
        "vector_retrieval",
        route_after_retrieval,
        {"llm_synthesis": "llm_synthesis", "fallback": "fallback"},
    )
    graph.add_edge("llm_synthesis", "format_citations")
    graph.add_edge("format_citations", END)
    graph.add_edge("fallback", END)
    return graph.compile()
