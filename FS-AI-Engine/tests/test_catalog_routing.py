"""Catalog questions must be answered from database records, whatever the spelling."""

import pytest
from engine.graphs.chat_analysis import QueryAnalysis
from engine.graphs.chat_graph import route_after_filters
from engine.graphs.chat_nodes import (
    build_catalog_records,
)

MEETINGS = [
    {"id": f"m{i}", "title": f"Meeting {i}", "project_id": "NOLOCODE"} for i in range(10)
] + [{"id": "m_free", "title": "Direct Test", "project_id": None}]
PROJECTS = [{"id": "NOLOCODE", "name": "Project NOLOCODE", "status": "ACTIVE"}]


def build(entities: set[str], intent_is_catalog: bool = True):
    return build_catalog_records(
        entities=entities,
        force_all=intent_is_catalog,
        available_projects=PROJECTS,
        available_meetings=MEETINGS,
        available_speakers=["Neha", "Danish khan"],
        scoped_to_meeting=False,
        project_id=None,
        has_speakers=False,
    )










def test_catalog_context_has_exact_counts_for_the_reported_query():
    context, instructions = build({"meetings", "projects"})
    assert "NOLOCODE: 10" in context and "Unassigned: 1" in context and "total meetings: 11" in context
    assert "Meetings: 10" in context  # per-project figure on the project line
    assert "Complete List of Meetings (11 total)" in context
    assert any("do not recount" in i for i in instructions)


def test_catalog_context_is_independent_of_wording():
    # No entity words at all, and the intent alone decides what is supplied
    context, _ = build(set(), intent_is_catalog=True)
    assert "Projects" in context and "Meetings" in context and "Participants" in context


def test_content_question_does_not_get_the_meeting_list():
    context, _ = build(set(), intent_is_catalog=False)
    assert "Complete List of Meetings" not in context


def test_catalog_questions_skip_retrieval_unless_they_ask_about_topics():
    assert route_after_filters({"skip_retrieval": True}) == "records"
    assert route_after_filters({"skip_retrieval": False}) == "rag"
    assert route_after_filters({}) == "rag"








@pytest.mark.asyncio
async def test_participant_involvement_pure_attendance_skips_retrieval(monkeypatch):
    from engine.graphs import chat_nodes

    async def mock_rewrite(query, history):
        return "What meetings was Harsh a part of?"

    async def mock_analyze(q, **_):
        return QueryAnalysis(intent="participant_involvement", speaker="Harsh Vardhan Dixit", needs_retrieval=False)

    monkeypatch.setattr(chat_nodes, "rewrite_query", mock_rewrite)
    monkeypatch.setattr(chat_nodes, "analyze_query", mock_analyze)
    monkeypatch.setattr(chat_nodes, "AsyncSessionLocal", None)

    state = {
        "query": "what meitngs was harsh part of?",
        "user_id": "test_user",
        "history": [],
    }
    result = await chat_nodes.extract_filters_node(state)
    assert result["query_intent"] == "participant_involvement"
    assert result["extracted_speaker"] == "Harsh Vardhan Dixit"
    # No attendance records could be loaded (no database), so retrieval must stay on as the fallback
    assert result["skip_retrieval"] is False
    assert route_after_filters(result) == "rag"


@pytest.mark.asyncio
async def test_participant_involvement_latest_meeting_skips_retrieval(monkeypatch):
    from engine.graphs import chat_nodes

    async def mock_rewrite(query, history):
        return "What was the most recent meeting Harsh was part of?"

    async def mock_analyze(q, **_):
        return QueryAnalysis(
            intent="participant_involvement", speaker="Harsh Vardhan Dixit", time_scope="latest", needs_retrieval=False
        )

    monkeypatch.setattr(chat_nodes, "rewrite_query", mock_rewrite)
    monkeypatch.setattr(chat_nodes, "analyze_query", mock_analyze)
    monkeypatch.setattr(chat_nodes, "AsyncSessionLocal", None)

    state = {
        "query": "what was the most recent meeting he was part of",
        "user_id": "test_user",
        "history": [{"role": "user", "content": "what meetings was harsh part of?"}],
    }
    result = await chat_nodes.extract_filters_node(state)
    assert result["extracted_speaker"] == "Harsh Vardhan Dixit"
    assert result["time_scope"] == "latest"
    # No attendance records could be loaded (no database), so retrieval must stay on as the fallback
    assert result["skip_retrieval"] is False
    assert route_after_filters(result) == "rag"


def test_catalog_query_includes_meetings_even_when_meeting_is_scoped():
    """Generic fix: Catalog queries must include the meeting list even if a specific meeting was inferred."""
    context, instructions = build_catalog_records(
        entities={"meetings"},
        force_all=True,
        available_projects=PROJECTS,
        available_meetings=MEETINGS,
        available_speakers=["Neha", "Danish khan"],
        scoped_to_meeting=True,  # Inferred by rewriter
        project_id=None,
        has_speakers=False,
    )
    assert "Complete List of Meetings (11 total)" in context
    assert any("The catalog lists 11 meetings in total" in i for i in instructions)


@pytest.mark.asyncio
@pytest.mark.parametrize("intent,needs_retrieval,expected", [
    ("catalog", False, True),          # pure inventory: answered from the database
    ("catalog", True, False),          # inventory plus topics: transcripts still needed
    ("content_qa", False, False),      # only catalog/attendance questions may skip retrieval
])
async def test_skip_retrieval_follows_the_analysis(monkeypatch, intent, needs_retrieval, expected):
    from engine.graphs import chat_nodes

    async def mock_rewrite(query, history):
        return query

    async def mock_analyze(q, **_):
        return QueryAnalysis(intent=intent, needs_retrieval=needs_retrieval)

    monkeypatch.setattr(chat_nodes, "rewrite_query", mock_rewrite)
    monkeypatch.setattr(chat_nodes, "analyze_query", mock_analyze)
    monkeypatch.setattr(chat_nodes, "AsyncSessionLocal", None)
    result = await chat_nodes.extract_filters_node({"query": "q", "user_id": "u", "history": []})
    assert result["skip_retrieval"] is expected


@pytest.mark.asyncio
async def test_smalltalk_is_answered_directly_without_retrieval(monkeypatch):
    from engine.graphs import chat_nodes
    from engine.graphs.chat_graph import smalltalk_node

    async def mock_rewrite(query, history):
        return query

    async def mock_analyze(q, **_):
        return QueryAnalysis(intent="smalltalk", needs_retrieval=False, reply="Hi there!")

    monkeypatch.setattr(chat_nodes, "rewrite_query", mock_rewrite)
    monkeypatch.setattr(chat_nodes, "analyze_query", mock_analyze)
    monkeypatch.setattr(chat_nodes, "AsyncSessionLocal", None)
    result = await chat_nodes.extract_filters_node({"query": "helo", "user_id": "u", "history": []})
    assert route_after_filters(result) == "smalltalk"
    assert (await smalltalk_node(result))["answer"] == "Hi there!"


@pytest.mark.asyncio
async def test_understand_subgraph_matches_sequential_extract_filters(monkeypatch):
    """The compiled understand subgraph and the sequential extract_filters_node must produce the same routing state."""
    from engine.graphs import chat_nodes
    from engine.graphs.chat_subgraphs import build_understand_subgraph

    async def mock_rewrite(query, history):
        return "What meetings was Harsh a part of?"

    async def mock_analyze(q, **_):
        return QueryAnalysis(intent="participant_involvement", speaker="Harsh Vardhan Dixit", needs_retrieval=False)

    monkeypatch.setattr(chat_nodes, "rewrite_query", mock_rewrite)
    monkeypatch.setattr(chat_nodes, "analyze_query", mock_analyze)
    monkeypatch.setattr(chat_nodes, "AsyncSessionLocal", None)

    state = {"query": "what meitngs was harsh part of?", "user_id": "test_user", "history": []}
    sequential = await chat_nodes.extract_filters_node(dict(state))
    graph_state = await build_understand_subgraph().ainvoke(dict(state))
    for key, value in sequential.items():
        assert graph_state[key] == value, key
