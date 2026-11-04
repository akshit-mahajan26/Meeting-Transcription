import pytest
from engine.graphs.chat_citations import build_citations, format_citations_node
from engine.graphs.chat_synthesis import split_sources_line


def test_split_sources_line_single_trailing():
    text = "This is a summary of the project.\nSOURCES: 1, 3"
    clean, reported = split_sources_line(text, n_chunks=5)
    assert clean == "This is a summary of the project."
    assert reported == [1, 3]


def test_split_sources_line_multiple_per_section_occurrences():
    """Reproduces the exact multi-meeting summary response from the trace."""
    text = (
        "* **Nolocode Meeting (April 29, 2026)**\n"
        "  Financial report export views discussed.\n"
        "  SOURCES: 1, 2, 3\n\n"
        "* **Akili And Sunfocus M4 (May 5, 2026)**\n"
        "  Document version retention evaluated.\n"
        "  SOURCES: 4, 5\n\n"
        "* **Akili X Sfs Standup (October 5, 2026)**\n"
        "  Team progress reviewed.\n"
        "  SOURCES: 26, 27, 28"
    )
    clean, reported = split_sources_line(text, n_chunks=30)

    # All SOURCES lines must be stripped from the clean answer
    assert "SOURCES:" not in clean
    assert "Nolocode Meeting (April 29, 2026)" in clean
    assert "Akili And Sunfocus M4 (May 5, 2026)" in clean
    assert "Akili X Sfs Standup (October 5, 2026)" in clean

    # All numbers across all 3 sections must be aggregated
    assert reported == [1, 2, 3, 4, 5, 26, 27, 28]


def test_split_sources_line_none_and_empty():
    text = "Hello! How can I help you today?\nSOURCES: none"
    clean, reported = split_sources_line(text, n_chunks=5)
    assert clean == "Hello! How can I help you today?"
    assert reported == []


def test_split_sources_line_out_of_bounds_filtered():
    text = "Answer here.\nSOURCES: 1, 2, 99"
    clean, reported = split_sources_line(text, n_chunks=5)
    assert clean == "Answer here."
    assert reported == [1, 2]


def test_split_sources_line_inline_markers_fallback():
    text = "Nolocode met [1] to discuss export views [2]."
    clean, reported = split_sources_line(text, n_chunks=5)
    assert clean == "Nolocode met to discuss export views."
    assert reported == [1, 2]


def test_build_citations_cross_meeting_collapses_per_meeting():
    chunks = [
        {"meeting_id": "mtg_1", "meeting_title": "Meeting 1", "speaker": "Alice", "text": "Discussion on architecture.", "start_time": 10.0, "end_time": 20.0, "similarity_score": 2.5},
        {"meeting_id": "mtg_1", "meeting_title": "Meeting 1", "speaker": "Bob", "text": "Scroll up.", "start_time": 30.0, "end_time": 35.0, "similarity_score": 2.8},  # filler downweighted
        {"meeting_id": "mtg_2", "meeting_title": "Meeting 2", "speaker": "Charlie", "text": "Database migrations completed.", "start_time": 50.0, "end_time": 60.0, "similarity_score": 1.9},
    ]
    citations = build_citations(chunks, meeting_scoped=False)
    assert len(citations) == 2
    # Meeting 1 should pick Alice's substantive turn over Bob's filler
    mtg1_cite = next(c for c in citations if c["meeting_id"] == "mtg_1")
    assert mtg1_cite["speaker"] == "Alice"
    assert "architecture" in mtg1_cite["exact_raw_text"]


@pytest.mark.asyncio
async def test_format_citations_node_provenance_parity():
    """Verifies that an un-cited but discussed meeting is anchored via provenance parity audit."""
    state = {
        "answer": "Here is the summary:\n* Nolocode Meeting: Discussed reports.\n* Akili And Sunfocus M4: Document upload handled.",
        "used_sources": [1],  # Model only cited chunk 1 (from Nolocode Meeting)
        "retrieved_chunks": [
            {"meeting_id": "mtg_1", "meeting_title": "Nolocode Meeting", "speaker": "Neha", "text": "Forecast summary table.", "start_time": 100.0, "end_time": 110.0, "similarity_score": 2.0},
            {"meeting_id": "mtg_2", "meeting_title": "Akili And Sunfocus M4", "speaker": "Ashpreet", "text": "Document upload automation.", "start_time": 200.0, "end_time": 210.0, "similarity_score": 1.8},
        ],
        "available_meetings": [
            {"id": "mtg_1", "title": "Nolocode Meeting"},
            {"id": "mtg_2", "title": "Akili And Sunfocus M4"},
        ],
        "meeting_id": None,
    }
    result = await format_citations_node(state)
    assert result["citations_count"] == 2
    c_mids = {c["meeting_id"] for c in result["citations"]}
    assert c_mids == {"mtg_1", "mtg_2"}


def _llm_mock(reply, seen=None):
    class Llm:
        async def generate_chat_completion(self, messages, **_):
            if seen is not None:
                seen.append(messages)
            if isinstance(reply, Exception):
                raise reply
            return reply

    return lambda: Llm()


@pytest.mark.asyncio
async def test_analysis_response_depth_classification(monkeypatch):
    import json

    from engine.graphs import chat_analysis
    from engine.graphs.chat_analysis import analyze_query

    # 1. Explanatory query
    seen = []
    reply_exp = json.dumps({
        "intent": "content_qa",
        "response_depth": "explanatory",
        "speaker": None,
        "meeting_id": None,
        "cross_meeting": False,
        "project_id": None,
        "time_scope": None,
        "date_from": None,
        "date_to": None,
        "catalog_entities": [],
        "needs_retrieval": True,
        "reply": None,
    })
    monkeypatch.setattr(chat_analysis, "get_llm_client", _llm_mock(reply_exp, seen))
    res = await analyze_query("can you explain it", available_speakers=[], available_meetings=[], available_projects=[])
    assert res.response_depth == "explanatory"
    assert res.intent == "content_qa"

    # 2. Concise query
    reply_concise = json.dumps({
        "intent": "content_qa",
        "response_depth": "concise",
        "speaker": None,
        "meeting_id": None,
        "cross_meeting": False,
        "project_id": None,
        "time_scope": None,
        "date_from": None,
        "date_to": None,
        "catalog_entities": [],
        "needs_retrieval": True,
        "reply": None,
    })
    monkeypatch.setattr(chat_analysis, "get_llm_client", _llm_mock(reply_concise))
    res2 = await analyze_query("how much effort was estimated?", available_speakers=[], available_meetings=[], available_projects=[])
    assert res2.response_depth == "concise"


@pytest.mark.asyncio
async def test_llm_synthesis_explanatory_depth_instructions(monkeypatch):
    from engine.graphs import chat_synthesis
    from engine.graphs.chat_synthesis import llm_synthesis_node

    seen = []
    monkeypatch.setattr(chat_synthesis, "get_llm_client", _llm_mock("Detailed explanation of Redis cache.\nSOURCES: 1", seen))

    state = {
        "query": "Can you explain the Redis cache fix in detail?",
        "response_depth": "explanatory",
        "retrieved_chunks": [
            {
                "meeting_id": "mtg_1",
                "meeting_title": "Sprint Sync",
                "speaker": "Karan Middha",
                "timestamp": "12:30",
                "topic_context": "Redis Cache",
                "text": "The microservice cache had no TTL and was coupled to Excel legacy sync.",
            }
        ],
        "available_meetings": [{"id": "mtg_1", "title": "Sprint Sync", "date": "2026-10-15"}],
    }

    result = await llm_synthesis_node(state)
    assert "Detailed explanation" in result["answer"]
    assert result["used_sources"] == [1]

    # Inspect the prompt messages passed to LLM
    assert len(seen) == 1
    system_msg = seen[0][0]["content"]
    user_msg = seen[0][-1]["content"]

    # System prompt must contain conversational progression rule and explanatory depth guideline
    assert "Conversational Progression" in system_msg
    assert "Response Depth & Brevity" in system_msg

    # User message must contain calibrated synthesis instructions and omit factual brevity restriction
    assert "FOCUSED SYNTHESIS" in user_msg
    assert "1 to 2 concise paragraphs" in user_msg
    assert "Keep it concise: 2-4 sentences" not in user_msg


@pytest.mark.asyncio
async def test_llm_synthesis_concise_depth_instructions(monkeypatch):
    from engine.graphs import chat_synthesis
    from engine.graphs.chat_synthesis import llm_synthesis_node

    seen = []
    monkeypatch.setattr(chat_synthesis, "get_llm_client", _llm_mock("It was estimated at 1.5 days.\nSOURCES: 1", seen))

    state = {
        "query": "What was the estimated development effort for the Redis cache fix?",
        "response_depth": "concise",
        "retrieved_chunks": [
            {
                "meeting_id": "mtg_1",
                "meeting_title": "Sprint Sync",
                "speaker": "Karan Middha",
                "timestamp": "12:30",
                "topic_context": "Redis Cache",
                "text": "Development effort is 1.5 days.",
            }
        ],
        "available_meetings": [{"id": "mtg_1", "title": "Sprint Sync", "date": "2026-10-15"}],
    }

    result = await llm_synthesis_node(state)
    assert result["answer"] == "It was estimated at 1.5 days."

    user_msg = seen[0][-1]["content"]
    assert "Keep it concise: 2-4 sentences" in user_msg
    assert "FOCUSED SYNTHESIS" not in user_msg


def test_rewrite_prompt_includes_followup_explanation_rule():
    from engine.graphs.chat_rewrite import REWRITE_PROMPT

    assert "explain" in REWRITE_PROMPT
    assert "underlying discussion" in REWRITE_PROMPT

