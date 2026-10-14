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

from app_pages import assistant, ingest, meetings, overview
from ui.auth import is_logged_in, render_auth_page
from ui.nav import register
from ui.shell import render_shell
from ui.state import init_state
from ui.styles import inject_styles

inject_styles()

# Nothing below runs until the user is logged in: each account only ever sees its own data.
if not is_logged_in():
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
