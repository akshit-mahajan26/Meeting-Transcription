# ruff: noqa: F811  (pytest fixtures are used as parameters named like their import)
"""Scope and records branches of the understand subgraph, on accounts with several meetings.

Reuses the `world` fixture of test_account_isolation (needs TEST_DATABASE_URL).
"""

import pytest
import pytest_asyncio
from engine.graphs import chat_nodes
from engine.graphs.chat_analysis import QueryAnalysis
from engine.graphs.chat_subgraphs import build_understand_subgraph
from sqlalchemy import text
from test_account_isolation import _ask, _stub_rewrite, needs_db, world  # noqa: F401  (pytest fixture)
from test_understand_subgraph import _stub_analysis

# ------------------------------- scope and records branches (several meetings per account) -------------------------------

DAY_MS = 86_400_000
BASE_MS = 1780383600000  # 2026-06-02 07:00 UTC, the meeting every `world` account already has


@pytest_asyncio.fixture
async def busy(world):
    """Account A also gets: an earlier meeting (May 23), and two meetings on June 12 (the later one has no Zed)."""
    accounts, llm = world
    a = accounts["a"]
    ids = {"early": f"early-{a['meeting']}", "mid": f"mid-{a['meeting']}", "late": f"late-{a['meeting']}"}
    dates = {"early": BASE_MS - 10 * DAY_MS, "mid": BASE_MS + 10 * DAY_MS, "late": BASE_MS + 10 * DAY_MS + 3_600_000}
    speakers = {"early": ["Neha"], "mid": ["Neha", "Zed"], "late": ["Neha"]}
    async with chat_nodes.AsyncSessionLocal() as s:
        for key, mid in ids.items():
            await s.execute(text(
                "INSERT INTO meetings (id,user_id,project_id,title,date,status,created_at,updated_at) "
                "VALUES (:m,:u,:p,:t,:d,'PROCESSED',now(),now())"),
                {"m": mid, "u": a["uid"], "p": a["project"], "t": f"{key} meeting {a['marker']}", "d": str(dates[key])})
            for n, spk in enumerate(speakers[key], start=1):
                await s.execute(text(
                    "INSERT INTO speaker_turns (turn_id,user_id,meeting_id,turn_number,speaker_id,speaker_name,text,"
                    "start_time,end_time,duration,sentence_indices,metadata) VALUES (:t,:u,:m,:n,'0',:sp,:tx,:ts,:ts,1,'[]',"
                    "CAST(:md AS jsonb))"),
                    {"t": f"{mid}_turn_{n:03d}", "u": a["uid"], "m": mid, "n": n, "ts": float(n), "sp": spk,
                     "tx": f"{spk} spoke in {key}", "md": f'{{"speaker": "{spk}"}}'})
        await s.commit()
    return accounts, llm, ids  # world teardown deletes the user, which cascades to these rows


async def _understand(user, **state):
    return await build_understand_subgraph().ainvoke({"query": "q", "user_id": user["uid"], "history": [], **state})


@needs_db
@pytest.mark.asyncio
@pytest.mark.parametrize("scope,expected,label", [
    ("latest", "late", "Most recent meeting"),
    ("earliest", "early", "Earliest meeting"),
])
async def test_latest_and_earliest_scope_pick_the_newest_and_oldest_meeting(busy, monkeypatch, scope, expected, label):
    accounts, _, ids = busy
    _stub_analysis(monkeypatch, QueryAnalysis(time_scope=scope))
    result = await _understand(accounts["a"])
    assert result["meeting_id"] == ids[expected] and result["reasoning"].startswith(label)
    assert result["meeting_scope_inferred"] is True


@needs_db
@pytest.mark.asyncio
async def test_latest_scope_for_a_speaker_only_considers_meetings_that_speaker_spoke_in(busy, monkeypatch):
    accounts, _, ids = busy
    _stub_analysis(monkeypatch, QueryAnalysis(speaker="Zed", time_scope="latest"))
    # Zed is not a known participant string here, so the analysis result is applied as given by the (mocked) analysis
    result = await _understand(accounts["a"])
    assert result["extracted_speakers"] == ["Zed"] and result["meeting_id"] == ids["mid"]  # newer "late" has no Zed


@needs_db
@pytest.mark.asyncio
async def test_cross_meeting_query_does_not_pick_a_single_meeting(busy, monkeypatch):
    accounts, _, _ = busy
    _stub_analysis(monkeypatch, QueryAnalysis(cross_meeting=True, time_scope="latest"))
    result = await _understand(accounts["a"])
    # (the "Cross-meeting query" reasoning text is only applied when a meeting is picked, which never happens here)
    assert result["meeting_id"] is None and result["meeting_scope_inferred"] is False
    assert result["reasoning"] == "Universal semantic vector search across meeting transcripts"


@needs_db
@pytest.mark.asyncio
async def test_two_meetings_on_one_day_are_both_searched(busy, monkeypatch):
    accounts, _, ids = busy
    _stub_analysis(monkeypatch, QueryAnalysis(date_from="2026-06-12", date_to="2026-06-12"))
    result = await _understand(accounts["a"])
    assert result["meeting_id"] is None and set(result["date_meeting_ids"]) == {ids["mid"], ids["late"]}
    assert result["meeting_scope_inferred"] is True and "2 meetings were held on" in result["reasoning"]


@needs_db
@pytest.mark.asyncio
async def test_analysis_can_name_a_meeting_and_a_project(busy, monkeypatch):
    accounts, _, ids = busy
    a = accounts["a"]
    _stub_analysis(monkeypatch, QueryAnalysis(meeting_id=ids["early"], project_id=a["project"]))
    result = await _understand(a)
    assert result["meeting_id"] == ids["early"] and result["project_id"] == a["project"]
    assert result["reasoning"].startswith("Scoped to meeting") and result["meeting_scope_inferred"] is True


@needs_db
@pytest.mark.asyncio
async def test_a_meeting_id_from_another_account_is_ignored(world, monkeypatch):
    accounts, _ = world
    a, b = accounts["a"], accounts["b"]
    _stub_analysis(monkeypatch, QueryAnalysis(meeting_id=b["meeting"]))
    result = await _understand(a)
    assert result["meeting_id"] is None


@needs_db
@pytest.mark.asyncio
@pytest.mark.parametrize("scope,expected,prefix", [
    ("latest", "late", "Most recent meeting for participant"),
    ("earliest", "early", "Earliest meeting for participant"),
])
async def test_attendance_latest_and_earliest_resolve_to_one_meeting(busy, monkeypatch, scope, expected, prefix):
    accounts, _, ids = busy
    _stub_analysis(monkeypatch, QueryAnalysis(
        intent="participant_involvement", speaker="Neha", time_scope=scope, needs_retrieval=False))
    result = await _understand(accounts["a"])
    assert [m["id"] for m in result["relevant_meetings"]] == [ids[expected]]
    assert result["meeting_id"] == ids[expected] and result["reasoning"].startswith(prefix)
    assert result["skip_retrieval"] is True


@needs_db
@pytest.mark.asyncio
async def test_attendance_lists_every_meeting_the_speaker_took_part_in(busy, monkeypatch):
    accounts, _, ids = busy
    a = accounts["a"]
    _stub_analysis(monkeypatch, QueryAnalysis(intent="participant_involvement", speaker="Neha", needs_retrieval=False))
    result = await _understand(a)
    assert {m["id"] for m in result["relevant_meetings"]} == {a["meeting"], *ids.values()}
    assert result["reasoning"].startswith("Found 4 meeting(s) for participant(s) Neha")


@needs_db
@pytest.mark.asyncio
async def test_attendance_for_a_speaker_with_no_turns_says_so_and_keeps_retrieval_on(busy, monkeypatch):
    accounts, _, _ = busy
    _stub_analysis(monkeypatch, QueryAnalysis(intent="participant_involvement", speaker="Nobody", needs_retrieval=False))
    result = await _understand(accounts["a"], extracted_speaker="Nobody")
    assert result["relevant_meetings"] == [] and result["skip_retrieval"] is False
    assert result["reasoning"] == "No meeting records found for participant(s): Nobody"


@needs_db
@pytest.mark.asyncio
async def test_project_participant_roster_is_limited_to_that_projects_speakers(busy, monkeypatch):
    accounts, _, _ = busy
    a = accounts["a"]
    _stub_analysis(monkeypatch, QueryAnalysis(
        intent="catalog", catalog_entities=["participants"], project_id=a["project"], needs_retrieval=False))
    result = await _understand(a)
    assert set(result["available_speakers"]) == {"Neha", "Zed"}
    assert result["reasoning"] == f"Catalog query: Found 2 participant(s) in project {a['project']}"


@needs_db
@pytest.mark.asyncio
@pytest.mark.parametrize("with_meeting", [False, True])
async def test_content_question_about_a_speaker_notes_the_filter(busy, monkeypatch, with_meeting):
    accounts, _, ids = busy
    _stub_analysis(monkeypatch, QueryAnalysis(speaker="Neha"))
    result = await _understand(accounts["a"], **({"meeting_id": ids["early"]} if with_meeting else {}))
    if with_meeting:
        assert result["reasoning"].endswith(" | Filtered to participant(s): Neha")
    else:
        assert result["reasoning"] == "Universal semantic search filtered to participant(s): Neha"
