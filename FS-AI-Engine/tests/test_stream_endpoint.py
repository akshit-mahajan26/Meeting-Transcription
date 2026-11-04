
"""The SSE streaming endpoint: event format, failures after the stream started, and access checks."""

import json
import re

from engine.api.routers import engine as engine_router
from engine.services.rag_service import RAGQueryResponse
from test_account_isolation import _stub_rewrite, needs_db, world  # noqa: F401  (pytest fixture)
from test_streaming import KEY, client

# ------------------------------- the SSE endpoint -------------------------------

def _parse_sse(raw: str):
    events = []
    for block in raw.strip().split("\n\n"):
        if not block.strip():
            continue
        name = re.search(r"^event: (.+)$", block, re.M).group(1)
        data = json.loads(re.search(r"^data: (.+)$", block, re.M).group(1))
        events.append((name, data))
    return events


def _fake_response():
    return RAGQueryResponse(query="q", answer="Final canonical.", citations=[], routing_mode="LANGGRAPH_RAG")


def _post_stream(fake, body=None, headers=None):
    engine_router_fake = fake
    body = body or {"query": "hi", "user_id": "u-1"}
    headers = {"X-Internal-Api-Key": KEY} if headers is None else headers
    original = engine_router.stream_user_query
    engine_router.stream_user_query = engine_router_fake
    try:
        return client.post("/api/v1/engine/query/stream", json=body, headers=headers)
    finally:
        engine_router.stream_user_query = original


def test_stream_endpoint_sends_status_tokens_and_final_as_server_sent_events():
    seen = {}

    async def fake(**kwargs):
        seen.update(kwargs)
        yield ("status", "Searching transcripts…")
        yield ("token", "Final ")
        yield ("token", "canonical.")
        yield ("final", _fake_response())

    res = _post_stream(fake, {"query": "hi", "user_id": "u-1", "history": [{"role": "user", "content": "earlier"}]})
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/event-stream")
    assert res.headers["cache-control"] == "no-cache" and res.headers["x-accel-buffering"] == "no"
    events = _parse_sse(res.text)
    assert [n for n, _ in events] == ["status", "token", "token", "final"]
    assert events[0][1] == {"message": "Searching transcripts…"} and events[1][1] == {"text": "Final "}
    assert events[-1][1]["answer"] == "Final canonical." and events[-1][1]["routing_mode"] == "LANGGRAPH_RAG"
    assert seen["user_id"] == "u-1" and seen["history"] == [{"role": "user", "content": "earlier"}]


def test_stream_endpoint_reports_a_failure_after_the_stream_started_as_an_error_event():
    async def fake(**_):
        yield ("status", "Understanding your question…")
        raise RuntimeError("secret internal detail")

    res = _post_stream(fake)
    assert res.status_code == 200
    events = _parse_sse(res.text)
    assert [n for n, _ in events] == ["status", "error"]
    assert "secret internal detail" not in res.text  # internals stay in the engine log


def test_stream_endpoint_needs_the_internal_key_and_a_user():
    async def fake(**_):
        yield ("final", _fake_response())

    assert _post_stream(fake, headers={}).status_code in (401, 403)
    assert _post_stream(fake, headers={"X-Internal-Api-Key": "wrong"}).status_code in (401, 403)
    assert _post_stream(fake, body={"query": "hi"}).status_code == 422
    assert _post_stream(fake, body={"query": "hi", "user_id": ""}).status_code == 422
