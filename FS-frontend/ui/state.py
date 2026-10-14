"""Session state and cached backend data access."""

from __future__ import annotations

from typing import Any

import streamlit as st
from services.api_client import api_client
from ui.auth import current_user_id
from utils.formatting import merge_projects

# st.cache_data is shared by every browser session, so each cached call is keyed by the account id:
# one account's cached meetings can never be served to another.


@st.cache_data(ttl=15, show_spinner=False)
def _meetings_for(user_id: str) -> list[dict[str, Any]]:
    return api_client.list_meetings()


@st.cache_data(ttl=30, show_spinner=False)
def _projects_for(user_id: str) -> list[str]:
    return api_client.list_projects()


@st.cache_data(ttl=60, show_spinner=False)
def _meeting_for(user_id: str, meeting_id: str) -> dict[str, Any] | None:
    return api_client.get_meeting(meeting_id)


def load_meetings() -> list[dict[str, Any]]:
    return _meetings_for(current_user_id())


def load_projects() -> list[str]:
    return _projects_for(current_user_id())


def load_meeting(meeting_id: str) -> dict[str, Any] | None:
    return _meeting_for(current_user_id(), meeting_id)


def refresh_data() -> None:
    st.cache_data.clear()


def init_state() -> None:
    st.session_state.setdefault("messages", [])
    st.session_state.setdefault("pending_query", None)
    st.session_state.setdefault("extra_projects", [])


def known_projects(meetings: list[dict[str, Any]]) -> list[str]:
    """Projects from the backend, from meetings, and any registered this session."""
    return merge_projects(
        load_projects(),
        (m.get("project_id") or "" for m in meetings),
        st.session_state.extra_projects,
    )


def register_project(project_id: str) -> None:
    project_id = project_id.strip()
    if project_id and project_id not in st.session_state.extra_projects:
        st.session_state.extra_projects.append(project_id)
