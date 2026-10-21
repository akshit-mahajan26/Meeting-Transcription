"""
FS-AI-Engine/graphs/chat_rewrite.py
===================================
The query-rewrite step: one small LLM call whose only job is to turn the user's message into a clear,
standalone question (typos fixed, follow-ups resolved). Routing decisions live in chat_analysis.py.
"""

from __future__ import annotations

import logging

from ..core.config import settings
from ..services.llm_client import get_llm_client

logger = logging.getLogger("fs-ai-engine.chat_graph")

REWRITE_PROMPT = """You rewrite the user's latest message into one clear, standalone question for a meeting-transcript assistant.

Rules:
- Fix typos and wrong words using the sentence context (e.g. "rate of the meeting" -> "date of the meeting", "meeitngs" -> "meetings").
- If the message cannot be understood without the earlier conversation (it uses "he", "she", "it", "they", "those", "that one", "his last meeting", "the other one", or is a fragment such as "why?" or "what about the second one?"), resolve the reference using the earlier user messages so the question stands on its own.
- When the message asks to explain, elaborate, or asks 'why' / 'how' regarding an earlier question (e.g. "can you explain it", "explain", "why?", "tell me more"), formulate the question to ask for an explanation of the underlying discussion, context, and reasons from that earlier topic rather than narrowing to an isolated metric or number.
- If the message is already clear, keep it unchanged apart from typo fixes. Never rewrite it into a different question.
- Greetings, thanks, goodbyes and questions about the assistant ("hello", "thanks", "what can you do") are never follow-ups: return them unchanged, whatever the earlier conversation was about.
- Only carry a name, meeting title or date over from the earlier conversation when the latest message points back to it. Never add names, titles or dates the latest message does not refer to.
- Never answer the question and never invent facts.

Reply with only the rewritten question, as plain text."""


async def rewrite_query(query: str, history: list[dict[str, str]] | None = None) -> str:
    """
    Returns the standalone, typo-corrected question. Falls back to the original message if the call fails
    or returns something unusable.
    """
    convo_lines = []
    # Only the user's own earlier questions: assistant answers can carry wrong names, titles and dates into the rewrite
    for m in [m for m in (history or []) if m.get("role") == "user"][-3:]:
        content = (m.get("content") or "").strip()
        if content:
            convo_lines.append(f"user: {content[:400]}")
    convo = "\n".join(convo_lines)

    try:
        reply = await get_llm_client().generate_chat_completion(
            messages=[
                {"role": "system", "content": REWRITE_PROMPT},
                {"role": "user", "content": f"Earlier user messages:\n{convo or '(none)'}\n\nLatest message: {query}"},
            ],
            model=settings.LLM_MODEL_NAME,
            max_tokens=200,
            temperature=0.0,
        )
    except Exception as exc:  # network error, ...
        logger.warning(f"[LANGGRAPH: REWRITE_QUERY] Keeping the original query: {exc}")
        return query

    rewritten = (reply or "").strip().strip('"').strip()
    if not rewritten or len(rewritten) > 4 * len(query) + 200:
        return query
    logger.info(f"[LANGGRAPH: REWRITE_QUERY] '{query}' -> '{rewritten}'")
    return rewritten
