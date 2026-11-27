"""
FS-frontend/app.py
==================
Entry point for the Meeting Intelligence Streamlit UI.

All communication with the platform goes through the FastAPI backend via
`services.api_client`; there is no direct database or LLM access here.

Layout:
    app_pages/  one module per page (overview, assistant, meetings, ingest)
    ui/         styles, shell, components, session state, shared scope panel
    utils/      pure formatting helpers
"""

import sys
from pathlib import Path

FRONTEND_DIR = Path(__file__).resolve().parent
if str(FRONTEND_DIR) not in sys.path:
    sys.path.insert(0, str(FRONTEND_DIR))

import streamlit as st

st.set_page_config(page_title="Meeting Intelligence", page_icon="🎙️", layout="wide", initial_sidebar_state="expanded")

from datetime import UTC, datetime

from app_pages import assistant, ingest, meetings, overview
from services import session_api
from services.api_client import SESSION_EXPIRED_KEY, TOKEN_KEY
from ui import session_cookie
from ui.auth import (
    EXPIRES_AT_KEY,
    LOGGED_OUT_KEY,
    REFRESH_AFTER_KEY,
    USER_KEY,
    is_logged_in,
    render_auth_page,
)
from ui.nav import register
from ui.shell import render_shell
from ui.state import init_state
from ui.styles import inject_styles

inject_styles()



def _passed(iso: str | None) -> bool:
    try:
        when = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return False
    return when.replace(tzinfo=when.tzinfo or UTC) <= datetime.now(UTC)


def _restore_session() -> None:
    """Log back in from the mi_session cookie (a refresh starts a new session state)."""
    state = st.session_state
    cookie = st.context.cookies.get(session_cookie.COOKIE_NAME)
    if state.get(TOKEN_KEY) or state.get(LOGGED_OUT_KEY) is True or state.get(SESSION_EXPIRED_KEY) or not cookie:
        return
    res = session_api.get_session(cookie)
    if res["success"]:
        data = res["data"]
        state[TOKEN_KEY] = cookie
        state[USER_KEY] = data["user"]
        state[EXPIRES_AT_KEY] = data.get("expires_at")
        state[REFRESH_AFTER_KEY] = data.get("refresh_after")
        state["cookie_token"] = cookie  # the browser already holds this cookie
    elif res["status_code"] == 401:
        state[SESSION_EXPIRED_KEY] = True


def _refresh_if_due() -> None:
    """Full runs only (fragment reruns never reach this file's top level)."""
    state = st.session_state
    if not state.get(REFRESH_AFTER_KEY) or not _passed(state[REFRESH_AFTER_KEY]):
        return
    res = session_api.refresh_token()
    if res["success"]:
        data = res["data"]
        state[TOKEN_KEY] = data["access_token"]
        state[EXPIRES_AT_KEY] = data.get("expires_at")
        state[REFRESH_AFTER_KEY] = data.get("refresh_after")
    elif res["status_code"] == 401:
        state.pop(TOKEN_KEY, None)
        state[SESSION_EXPIRED_KEY] = True


_restore_session()
if is_logged_in():
    _refresh_if_due()
if is_logged_in():
    if st.session_state.get("cookie_token") != st.session_state[TOKEN_KEY]:
        session_cookie.write(st.session_state[TOKEN_KEY], st.session_state.get(EXPIRES_AT_KEY))
        st.session_state["cookie_token"] = st.session_state[TOKEN_KEY]

# Nothing below runs until the user is logged in: each account only ever sees its own data.
if not is_logged_in():
    if st.session_state.get(SESSION_EXPIRED_KEY):
        session_cookie.delete()
        st.session_state[LOGGED_OUT_KEY] = True
    elif st.session_state.get(LOGGED_OUT_KEY) and not st.session_state.get("cookie_cleared"):
        session_cookie.delete()  # in case the deleter mounted by logout() never rendered before its rerun
        st.session_state["cookie_cleared"] = True
    render_auth_page()
    st.stop()

init_state()

pages = {
    "overview": st.Page(overview.render, title="Overview", icon=":material/dashboard:", url_path="overview", default=True),
    "assistant": st.Page(assistant.render, title="Assistant", icon=":material/auto_awesome:", url_path="assistant"),
    "meetings": st.Page(meetings.render, title="Meetings", icon=":material/video_library:", url_path="meetings"),
    "ingest": st.Page(ingest.render, title="Ingest", icon=":material/cloud_upload:", url_path="ingest"),
}
register(pages)
nav = st.navigation(list(pages.values()), position="top")
render_shell()
nav.run()

# A backend call answered 401 during this run (expired/invalid token): go back to the login page.
if st.session_state.get("session_expired"):
    st.rerun()
