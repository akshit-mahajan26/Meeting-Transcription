"""Assistant page: grounded Q&A over transcripts."""

from __future__ import annotations

import streamlit as st
from config import frontend_config
from services.api_client import api_client
from ui.components import hero, render_assistant_message
from ui.scope import Scope, render_scope_panel
from ui.state import known_projects, load_meetings

SUGGESTIONS = [
    (":material/gavel:", "Key decisions", "What key decisions were ratified in the meetings?"),
    (":material/warning:", "Blockers & risks", "Are there any unresolved blockers or risks mentioned in the transcripts?"),
    (":material/checklist:", "Action items", "Summarize all commitments and action items with their respective owners."),
    (":material/groups:", "Participants & topics", "Who were the meeting participants and what topics were discussed?"),
]


def build_chat_history(messages: list[dict], max_questions: int = 5) -> list[dict[str, str]]:
    """The most recent `max_questions` user questions with the answers that followed (role + text only)."""
    if max_questions <= 0:
        return []
    usable = [m for m in messages if m.get("content") and not m.get("error")]
    asked = [i for i, m in enumerate(usable) if m["role"] == "user"]
    start = asked[-max_questions] if len(asked) > max_questions else 0
    return [{"role": m["role"], "content": m["content"]} for m in usable[start:]]


def _ask(question: str, scope: Scope, chat_history: list[dict[str, str]]) -> dict:
    res = api_client.query_backend(
        question, project_id=scope.project_id, speaker=scope.speaker, meeting_id=scope.meeting_id, history=chat_history
    )
    if not res.get("success", True):
        return {"role": "assistant", "error": res.get("error", "An error occurred."), "content": res["answer"], "citations": []}
    return {
        "role": "assistant",
        "content": res.get("answer", "No response synthesized."),
        "citations": res.get("citations", []),
        "routing_mode": res.get("routing_mode"),
        "routing_reasoning": res.get("routing_reasoning", ""),
        "llm_model_used": res.get("llm_model_used"),
    }


def _ask_streaming(question: str, scope: Scope, chat_history: list[dict[str, str]]) -> dict:
    """
    Writes the answer into the chat as it arrives and returns the finished message, like _ask. The `final` event
    holds the canonical answer and citations; the page re-renders from it after the stream, so any difference to
    the streamed text (the model's source markers are removed there) settles on the canonical text.
    """
    status = st.empty()
    status.caption("Thinking…")
    final: dict = {}
    failed: dict = {}

    def tokens():
        waiting = True
        for event in api_client.query_backend_stream(
            question, project_id=scope.project_id, speaker=scope.speaker, meeting_id=scope.meeting_id, history=chat_history
        ):
            kind = event["event"]
            if kind == "status":
                status.caption(event["message"])
            elif kind == "token":
                if waiting:
                    status.empty()
                    waiting = False
                yield event["text"]
            elif kind == "final":
                final.update(event["data"])
            else:
                failed.update(event)

    st.write_stream(tokens())
    status.empty()

    if final:
        return {
            "role": "assistant",
            "content": final.get("answer", "No response synthesized."),
            "citations": final.get("citations", []),
            "routing_mode": final.get("routing_mode"),
            "routing_reasoning": final.get("routing_reasoning", ""),
            "llm_model_used": final.get("llm_model_used"),
        }
    if failed.get("status_code") in (404, 405):  # a backend without the streaming endpoint: use the plain request
        return _ask(question, scope, chat_history)
    return {"role": "assistant", "error": failed.get("error", "An error occurred."), "content": failed.get("answer", ""), "citations": []}


def _render(msg: dict) -> None:
    with st.chat_message(msg["role"], avatar=":material/person:" if msg["role"] == "user" else ":material/smart_toy:"):
        if msg["role"] == "assistant":
            if msg.get("error"):
                st.error(msg["error"])
            render_assistant_message(msg)
        else:
            st.markdown(msg["content"])


def render() -> None:
    meetings = load_meetings()
    scope = render_scope_panel(meetings, known_projects(meetings))
    messages = st.session_state.messages
    box = st.container(height=560, key="chat_container", border=False)

    with box:
        if not messages:
            hero()
            with st.container(key="suggestions"):
                cols = st.columns(2)
                for i, (icon, title, question) in enumerate(SUGGESTIONS):
                    if cols[i % 2].button(f"**{title}**  \n{question}", icon=icon, key=f"sugg_{i}", width="stretch"):
                        st.session_state.pending_query = question
                        st.rerun()
        for msg in messages:
            _render(msg)

    pending = st.session_state.pop("pending_query", None)
    typed = st.chat_input("Ask about decisions, topics, or what someone said…")
    question = (pending or typed or "").strip()

    with st.bottom:
        st.markdown(
            '<div class="disclaimer">Answers can contain mistakes. Verify critical facts against the cited sources.</div>',
            unsafe_allow_html=True,
        )

    if question:
        chat_history = build_chat_history(messages)  # taken before the new question is appended; use [] to turn history off
        user_msg = {"role": "user", "content": question}
        messages.append(user_msg)
        with box:
            _render(user_msg)
            with st.chat_message("assistant", avatar=":material/smart_toy:"):
                if frontend_config.STREAM_RESPONSES:
                    answer = _ask_streaming(question, scope, chat_history)
                else:
                    with st.spinner("Searching transcripts and synthesizing a grounded answer…"):
                        answer = _ask(question, scope, chat_history)
        messages.append(answer)
        st.rerun()  # history loop above renders the stored answer

