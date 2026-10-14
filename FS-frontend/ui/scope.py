"""Retrieval scope (meeting / project / speaker) shared between pages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import streamlit as st
from utils.formatting import chat_to_markdown, meeting_label


@dataclass
class Scope:
    meeting_id: str | None = None
    project_id: str | None = None
    speaker: str | None = None

    @property
    def active_count(self) -> int:
        return sum(1 for v in (self.meeting_id, self.project_id, self.speaker) if v)


def set_scope(meeting_id: str | None = None, project_id: str | None = None) -> None:
    """Pre-set the assistant scope from another page."""
    st.session_state.scope_meeting_id = meeting_id
    st.session_state.scope_project_id = project_id


def render_scope_panel(meetings: list[dict[str, Any]], projects: list[str]) -> Scope:
    """Sidebar panel for the assistant page. Selections persist across pages in session_state."""
    with st.sidebar:
        st.markdown('<div class="section-label">Retrieval scope</div>', unsafe_allow_html=True)

        ids = [None] + [m["id"] for m in meetings]
        labels = {m["id"]: meeting_label(m) for m in meetings}
        current = st.session_state.get("scope_meeting_id")
        meeting_id = st.selectbox(
            "Meeting", ids, index=ids.index(current) if current in ids else 0,
            format_func=lambda i: "All meetings" if i is None else labels[i],
            help="Lock searches to one meeting, or leave on 'All meetings' for automatic detection.",
        )
        st.session_state.scope_meeting_id = meeting_id

        proj_opts = [None, *projects]
        current_p = st.session_state.get("scope_project_id")
        project_id = st.selectbox(
            "Project", proj_opts, index=proj_opts.index(current_p) if current_p in proj_opts else 0,
            format_func=lambda p: "All projects" if p is None else p,
            help="Restrict citations and synthesis to one project.",
        )
        st.session_state.scope_project_id = project_id

        speaker = st.text_input("Speaker", placeholder="e.g. Harsh or Neha", help="Focus on one participant.").strip() or None

        scope = Scope(meeting_id, project_id, speaker)
        if scope.active_count:
            st.caption(f":material/filter_alt: {scope.active_count} filter(s) active")

        st.markdown('<div class="section-label">Conversation</div>', unsafe_allow_html=True)
        has = bool(st.session_state.messages)
        if st.button("Clear conversation", icon=":material/delete:", width="stretch", disabled=not has):
            st.session_state.messages = []
            st.toast("Conversation cleared", icon=":material/check_circle:")
            st.rerun()
        if has:
            st.download_button(
                "Export as Markdown", chat_to_markdown(st.session_state.messages),
                file_name="meeting-intelligence-chat.md", mime="text/markdown",
                icon=":material/download:", width="stretch",
            )
    return scope
