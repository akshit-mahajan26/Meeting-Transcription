"""Conversation history reaches the LLM as prior chat turns, ahead of the current question."""

import pytest
from engine.graphs import chat_rewrite, chat_synthesis


class _Llm:
    provider = "test"

    def __init__(self):
        self.messages = None

    async def generate_chat_completion(self, messages, **_):
        self.messages = messages
        return "ok\nSOURCES: none"


@pytest.mark.asyncio
async def test_history_is_sent_between_system_prompt_and_question(monkeypatch):
    llm = _Llm()
    monkeypatch.setattr(chat_synthesis, "get_llm_client", lambda: llm)
    monkeypatch.setattr(chat_rewrite, "get_llm_client", lambda: llm)
    history = [
        {"role": "user", "content": "who attended the S3 meeting?"},
        {"role": "assistant", "content": "Neha and Karan."},
        {"role": "tool", "content": "ignored"},
    ]
    state = {"query": "and when was it?", "query_intent": "content_qa", "history": history, "retrieved_chunks": []}
    out = await chat_synthesis.llm_synthesis_node(state)
    roles = [m["role"] for m in llm.messages]
    assert roles == ["system", "user", "assistant", "user"]
    assert llm.messages[1]["content"] == "who attended the S3 meeting?"
    assert "and when was it?" in llm.messages[-1]["content"]
    assert out["answer"] == "ok"


@pytest.mark.asyncio
async def test_no_history_leaves_messages_unchanged(monkeypatch):
    llm = _Llm()
    monkeypatch.setattr(chat_synthesis, "get_llm_client", lambda: llm)
    monkeypatch.setattr(chat_rewrite, "get_llm_client", lambda: llm)
    await chat_synthesis.llm_synthesis_node({"query": "hi", "query_intent": "content_qa", "retrieved_chunks": []})
    assert [m["role"] for m in llm.messages] == ["system", "user"]
