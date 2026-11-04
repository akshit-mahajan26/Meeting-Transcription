
import pytest
from engine.graphs import chat_rewrite, chat_synthesis
from engine.graphs.chat_analysis import QueryAnalysis
from engine.graphs.chat_nodes import meetings_in_date_range
from engine.graphs.chat_records import build_meeting_details

AVAILABLE_MEETINGS = [
    {"id": "01KQBRC3M5TCY9WDC7SQVV3X6F", "title": "Nolocode Meeting", "project_id": "NOLOCODE", "date": "2026-04-29 04:35 UTC"},
    {"id": "01KTRHTDCKY1BTKCACQS2VP3YM", "title": "Akili X Sfs Review Meeting", "project_id": "NOLOCODE", "date": "2026-06-12 08:45 UTC"},
    {"id": "01KTXPCKMGW63E2C0B9A3FGKDV", "title": "M4 And Devops Discussion", "project_id": "NOLOCODE", "date": "2026-06-12 10:30 UTC"},
    {"id": "01KVACW7CKTSBB3KS78VB94B37", "title": "S3 Discussion With Ashpreet", "project_id": "NOLOCODE", "date": "2026-06-17 09:30 UTC"},
    {"id": "01KX03T7EG53T8FNA0M4QFB56A", "title": "Akili X Sfs Standup 1Pm Uae", "project_id": "NOLOCODE", "date": "2026-07-08 09:00 UTC"},
    {"id": "01KZN0WZE56ATNE0722ATGSKEF", "title": "Akili X Sfs Standup 9Am Uae", "project_id": "NOLOCODE", "date": "2026-08-10 06:00 UTC"},
    {"id": "01M3KAS28DC46FK7YMCJ0V875N", "title": "Akili X Sfs Standup 9Am Uae", "project_id": "NOLOCODE", "date": "2026-09-30 05:00 UTC"},
    {"id": "01M406RQ7640XDR6P42D6ES4DK", "title": "Akili X Sfs Standup 9Am Uae", "project_id": "NOLOCODE", "date": "2026-10-05 05:00 UTC"},
]







def test_build_meeting_details_orders_newest_first_and_includes_all():
    """Verifies that meeting details sort chronologically descending (newest first)."""
    meeting_ids = [
        "01KQBRC3M5TCY9WDC7SQVV3X6F",  # April
        "01KTXPCKMGW63E2C0B9A3FGKDV",  # June
        "01M406RQ7640XDR6P42D6ES4DK",  # October
    ]
    details = build_meeting_details(AVAILABLE_MEETINGS, meeting_ids)

    # October meeting must appear before June and April
    oct_idx = details.find("2026-10-05")
    june_idx = details.find("2026-06-12")
    apr_idx = details.find("2026-04-29")

    assert oct_idx != -1
    assert june_idx != -1
    assert apr_idx != -1
    assert oct_idx < june_idx < apr_idx


def _held(date_from, date_to):
    matched, label = meetings_in_date_range(AVAILABLE_MEETINGS, date_from, date_to)
    return [m["id"] for m in matched], label


@pytest.mark.parametrize("day,expected_id", [
    ("2026-10-05", "01M406RQ7640XDR6P42D6ES4DK"),
    ("2026-06-17", "01KVACW7CKTSBB3KS78VB94B37"),
    ("04-29", "01KQBRC3M5TCY9WDC7SQVV3X6F"),
])
def test_a_single_day_resolves_to_the_meeting_held_that_day(day, expected_id):
    ids, label = _held(day, day)
    assert ids == [expected_id]
    assert label


def test_a_day_with_several_meetings_returns_all_of_them_oldest_first():
    matched, label = meetings_in_date_range(AVAILABLE_MEETINGS, "06-12", "06-12")
    assert [m["title"] for m in matched] == ["Akili X Sfs Review Meeting", "M4 And Devops Discussion"]
    assert label == "2026-06-12"


def test_a_day_without_a_year_uses_the_newest_year_that_has_a_meeting_that_day():
    older = {"id": "old", "title": "Old Review", "project_id": None, "date": "2025-06-17 09:00 UTC"}
    matched, _ = meetings_in_date_range([older, *AVAILABLE_MEETINGS], "06-17", "06-17")
    assert [m["id"] for m in matched] == ["01KVACW7CKTSBB3KS78VB94B37"]
    matched, _ = meetings_in_date_range([older, *AVAILABLE_MEETINGS], "2025-06-17", "2025-06-17")
    assert [m["id"] for m in matched] == ["old"]


@pytest.mark.parametrize("date_from,date_to,expected_days", [
    ("2026-10-03", None, ["2026-10-05"]),  # "after October 2"
    (None, "2026-05-01", ["2026-04-29"]),  # "before May 1"
    ("2026-06-12", "2026-07-08", ["2026-06-12", "2026-06-12", "2026-06-17", "2026-07-08"]),  # "between ..."
    ("2026-07-08", "2026-06-12", ["2026-06-12", "2026-06-12", "2026-06-17", "2026-07-08"]),  # reversed bounds
])
def test_date_ranges_include_every_meeting_inside_them(date_from, date_to, expected_days):
    matched, _ = meetings_in_date_range(AVAILABLE_MEETINGS, date_from, date_to)
    assert [m["date"][:10] for m in matched] == expected_days


def test_a_range_with_no_meetings_is_reported_not_guessed():
    matched, label = meetings_in_date_range(AVAILABLE_MEETINGS, "2026-10-06", None)
    assert matched == []
    assert label == "2026-10-06 onwards"


@pytest.mark.parametrize("date_from,date_to", [(None, None), ("", ""), ("02-30", "02-30"), ("not a date", None)])
def test_missing_or_unusable_dates_leave_the_question_unfiltered(date_from, date_to):
    assert meetings_in_date_range(AVAILABLE_MEETINGS, date_from, date_to) == ([], None)






def test_system_prompt_contains_temporal_grounding():
    """Verifies that get_system_prompt includes current date and strict temporal boundaries."""
    from datetime import UTC, datetime

    from engine.graphs.chat_synthesis import get_system_prompt

    test_now = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
    prompt = get_system_prompt(test_now)

    assert "2026-10-14" in prompt
    assert "Wednesday" in prompt
    assert "Real-Time Temporal" in prompt or "System Temporal Reference" in prompt
    assert "NEVER confuse" in prompt
















@pytest.mark.asyncio
async def test_llm_synthesis_labels_speaker_vs_mention(monkeypatch):
    captured_prompt = []

    class Llm:
        async def generate_chat_completion(self, messages, **_):
            for m in messages:
                if m.get("role") == "user":
                    captured_prompt.append(m.get("content", ""))
            return "Answer"

    monkeypatch.setattr(chat_synthesis, "get_llm_client", lambda: Llm())

    monkeypatch.setattr(chat_rewrite, "get_llm_client", lambda: Llm())

    chunks = [
        {
            "meeting_id": "m1",
            "meeting_title": "Standup A",
            "date": "2026-07-08",
            "speaker": "Harsh Vardhan Dixit",
            "timestamp": "00:10:00",
            "text": "I am working on the chatbot UI.",
            "topic_context": "Chatbot UI",
        },
        {
            "meeting_id": "m2",
            "meeting_title": "Standup B",
            "date": "2026-10-05",
            "speaker": "Bhavneet Mhajan",
            "timestamp": "00:01:00",
            "text": "Sorry is harsh not joining her.",
            "topic_context": "Attendance Check",
        },
    ]

    state = {
        "query": "What did Harsh say?",
        "extracted_speaker": "Harsh Vardhan Dixit",
        "extracted_speakers": ["Harsh Vardhan Dixit"],
        "retrieved_chunks": chunks,
        "available_meetings": [
            {"id": "m1", "title": "Standup A", "date": "2026-07-08"},
            {"id": "m2", "title": "Standup B", "date": "2026-10-05"},
        ],
    }

    await chat_synthesis.llm_synthesis_node(state)
    prompt_text = "\n".join(captured_prompt)

    assert "[PARTICIPANT SPOKE: Harsh Vardhan Dixit spoke this turn]" in prompt_text
    assert "[PARTICIPANT MENTION ONLY: Spoken by Bhavneet Mhajan, NOT by Harsh Vardhan Dixit]" in prompt_text
    assert "Attendance & Speaker Attribution" in prompt_text







@pytest.mark.asyncio
async def test_zero_attendance_participant_involvement_prompt(monkeypatch):
    captured_prompt = []

    class Llm:
        async def generate_chat_completion(self, messages, **_):
            for m in messages:
                if m.get("role") == "user":
                    captured_prompt.append(m.get("content", ""))
            return "Sarah has not attended any recorded meetings."

    monkeypatch.setattr(chat_synthesis, "get_llm_client", lambda: Llm())

    state = {
        "query": "Which meetings did Sarah attend?",
        "query_intent": "participant_involvement",
        "extracted_speaker": "Sarah",
        "extracted_speakers": ["Sarah"],
        "relevant_meetings": [],
        "available_meetings": [],
    }

    await chat_synthesis.llm_synthesis_node(state)
    prompt_text = "\n".join(captured_prompt)

    assert "Database Records - Verified Meetings with Participant(s) Sarah (0 total):" in prompt_text
    assert "No recorded meetings found where Sarah attended or spoke." in prompt_text
    assert "State directly and clearly that Sarah has not attended or spoken in any recorded meetings." in prompt_text


@pytest.mark.asyncio
async def test_latest_meeting_attendance_prompt(monkeypatch):
    captured_prompt = []

    class Llm:
        async def generate_chat_completion(self, messages, **_):
            for m in messages:
                if m.get("role") == "user":
                    captured_prompt.append(m.get("content", ""))
            return "Answer"

    monkeypatch.setattr(chat_synthesis, "get_llm_client", lambda: Llm())

    state = {
        "query": "What was the most recent meeting Harsh was part of?",
        "query_intent": "participant_involvement",
        "time_scope": "latest",
        "extracted_speaker": "Harsh Vardhan Dixit",
        "extracted_speakers": ["Harsh Vardhan Dixit"],
        "relevant_meetings": [
            {"id": "m_latest", "title": "Akili X Sfs Standup 1Pm Uae", "date": "08 July 2026", "turns": 27}
        ],
        "available_meetings": [
            {"id": "m_latest", "title": "Akili X Sfs Standup 1Pm Uae", "date": "08 July 2026"}
        ],
    }

    await chat_synthesis.llm_synthesis_node(state)
    prompt_text = "\n".join(captured_prompt)

    assert "Database Records - Most Recent Verified Meeting with Participant Harsh Vardhan Dixit:" in prompt_text
    assert "Akili X Sfs Standup 1Pm Uae" in prompt_text
    assert "The user is asking for the most recent meeting Harsh Vardhan Dixit attended / was part of." in prompt_text
    assert "Do NOT mention any subsequent meeting where Harsh Vardhan Dixit was absent or only mentioned." in prompt_text


@pytest.mark.asyncio
async def test_earliest_meeting_attendance_prompt(monkeypatch):
    captured_prompt = []

    class Llm:
        async def generate_chat_completion(self, messages, **_):
            for m in messages:
                if m.get("role") == "user":
                    captured_prompt.append(m.get("content", ""))
            return "Answer"

    monkeypatch.setattr(chat_synthesis, "get_llm_client", lambda: Llm())

    state = {
        "query": "What was the first meeting Harsh attended?",
        "query_intent": "participant_involvement",
        "time_scope": "earliest",
        "extracted_speaker": "Harsh Vardhan Dixit",
        "extracted_speakers": ["Harsh Vardhan Dixit"],
        "relevant_meetings": [
            {"id": "m_first", "title": "Nolocode Meeting", "date": "29 April 2026", "turns": 15}
        ],
        "available_meetings": [
            {"id": "m_first", "title": "Nolocode Meeting", "date": "29 April 2026"}
        ],
    }

    await chat_synthesis.llm_synthesis_node(state)
    prompt_text = "\n".join(captured_prompt)

    assert "Database Records - Earliest Verified Meeting with Participant Harsh Vardhan Dixit:" in prompt_text
    assert "Nolocode Meeting" in prompt_text
    assert "The user is asking for the earliest meeting Harsh Vardhan Dixit attended / was part of." in prompt_text
    assert "Do NOT mention any subsequent meeting where Harsh Vardhan Dixit was absent or only mentioned." in prompt_text


@pytest.mark.asyncio
@pytest.mark.parametrize("query,expected_id", [
    ("who led the first meeting?", "01KQBRC3M5TCY9WDC7SQVV3X6F"),
    ("what happened in the earliest call", "01KQBRC3M5TCY9WDC7SQVV3X6F"),
    ("can you summarize the last meeting?", "01M406RQ7640XDR6P42D6ES4DK"),
    ("who led the second meeting?", "01KTRHTDCKY1BTKCACQS2VP3YM"),
])
async def test_extract_filters_resolves_ordinal_and_relative_queries(monkeypatch, query, expected_id):
    """Verifies that extract_filters_node scopes ordinal and relative queries generically via rewrite meeting_id."""
    from engine.graphs import chat_nodes

    async def mock_rewrite(q, history):
        return q

    async def mock_analyze(q, **_):
        return QueryAnalysis(intent="content_qa", meeting_id=expected_id)

    monkeypatch.setattr(chat_nodes, "rewrite_query", mock_rewrite)
    monkeypatch.setattr(chat_nodes, "analyze_query", mock_analyze)
    monkeypatch.setattr(chat_nodes, "AsyncSessionLocal", None)

    state = {
        "query": query,
        "user_id": "test_user",
        "history": [],
        "available_meetings": AVAILABLE_MEETINGS,
    }
    result = await chat_nodes.extract_filters_node(state)
    assert result["meeting_id"] == expected_id






@pytest.mark.asyncio
async def test_extract_filters_node_preserves_conversation_history(monkeypatch):
    """Verifies that extract_filters_node preserves history for synthesis even when follow_up is false."""
    from engine.graphs import chat_nodes

    async def mock_rewrite(query, history):
        return "What was the deployment timeline?"

    async def mock_analyze(q, **_):
        return QueryAnalysis()

    monkeypatch.setattr(chat_nodes, "rewrite_query", mock_rewrite)
    monkeypatch.setattr(chat_nodes, "analyze_query", mock_analyze)
    monkeypatch.setattr(chat_nodes, "AsyncSessionLocal", None)

    history = [
        {"role": "user", "content": "What was discussed in the Nolocode meeting?"},
        {"role": "assistant", "content": "The team discussed deployment and architecture."},
    ]
    state = {
        "query": "Tell me more about the deployment",
        "user_id": "test_user",
        "history": history,
    }
    result = await chat_nodes.extract_filters_node(state)
    assert result["history"] == history


@pytest.mark.asyncio
async def test_vector_retrieval_node_handles_empty_embedding(monkeypatch):
    """Verifies that vector_retrieval_node does not crash or generate invalid vector cast when query_embedding is empty."""
    from engine.graphs import chat_retrieval

    class FakeResult:
        def fetchall(self):
            return []

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def execute(self, statement, params=None):
            sql_text = str(statement)
            # Ensure no CAST(:q_vec AS vector) is in the SQL when embedding is empty
            assert "CAST(:q_vec AS vector)" not in sql_text
            assert "q_vec" not in (params or {})
            return FakeResult()

    monkeypatch.setattr(chat_retrieval, "AsyncSessionLocal", lambda: FakeSession())

    state = {
        "query": "Who was handling the M4 UAT points?",
        "query_embedding": [],  # empty vector from failed embed
        "user_id": "test_user",
    }
    result = await chat_retrieval.vector_retrieval_node(state)
    assert "retrieved_chunks" in result

