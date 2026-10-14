"""Ingest page."""

from __future__ import annotations

import streamlit as st
from services.api_client import api_client
from ui.components import page_header
from ui.state import known_projects, load_meetings, refresh_data, register_project

ADD_NEW = "➕ Add new project…"

STEPS = [
    ("Paste a link", "A Fireflies dashboard URL or a direct .mp3 / .mp4 / .wav / .m4a recording."),
    ("Pick a project", "Choose an existing project ID or register a new one."),
    ("Process in background", "FS-AI-Engine transcribes and normalizes turns asynchronously."),
    ("Explore & ask", "Open the Transcript explorer or ask questions in Chat."),
]


def render() -> None:
    projects = known_projects(load_meetings())
    page_header("Ingest a meeting", "Submit a recording or Fireflies link for background transcription.")
    form_col, help_col = st.columns([3, 2], gap="large")

    with form_col:
        link = st.text_input(
            "Meeting link",
            placeholder="https://app.fireflies.ai/view/Meeting-Title::01KT43KCH4XRVKJT1E65YNT051",
            help="Supports Fireflies dashboard links or direct media URLs.",
        )
        title = st.text_input(
            "Custom title (optional)",
            placeholder="Leave blank to derive the title from the URL slug",
        )

        options = [*projects, ADD_NEW]
        default = st.session_state.get("ingest_project")
        choice = st.selectbox(
            "Target project",
            options,
            index=options.index(default) if default in options else 0,
            help="Select an existing project to avoid typos, or add a new one.",
        )
        project_id = choice
        if choice == ADD_NEW:
            project_id = st.text_input("New project ID", placeholder="e.g. proj-marketing-q4").strip()
            if not project_id:
                st.caption("Enter the new project ID to continue.")

        if st.button("Enqueue ingestion", type="primary", icon=":material/rocket_launch:", width="stretch"):
            if not link.strip():
                st.warning("Please provide a meeting link.", icon=":material/link_off:")
            elif not project_id:
                st.warning("Please enter a project ID.", icon=":material/folder_off:")
            else:
                with st.status("Submitting to backend…", expanded=True) as status:
                    res = api_client.ingest_link(link.strip(), project_id=project_id, title=title or None)
                    if res.get("success"):
                        register_project(project_id)
                        st.session_state.ingest_project = project_id
                        refresh_data()
                        status.update(label="Accepted for background processing", state="complete")
                        st.json(res.get("data", {}), expanded=False)
                    else:
                        status.update(label="Ingestion failed", state="error")
                        st.error(res.get("error"))
                if res.get("success"):
                    st.toast(f"Queued for project {project_id}", icon=":material/check_circle:")
                    st.caption("Processing is asynchronous. Check the **Meetings** page in a moment to see new meetings.")

    with help_col:
        with st.container(border=True):
            st.markdown("**How it works**")
            for i, (t, d) in enumerate(STEPS, 1):
                st.markdown(
                    f'<div class="step"><div class="step-n">{i}</div>'
                    f'<div><div class="step-t">{t}</div><div class="step-d">{d}</div></div></div>',
                    unsafe_allow_html=True,
                )
        with st.expander("Supported URL formats", icon=":material/link:"):
            st.markdown(
                "- `https://app.fireflies.ai/view/Slug::TranscriptID`\n"
                "- `https://app.fireflies.ai/view/01KT43KCH4XRVKJT1E65YNT051`\n"
                "- Direct `.mp3`, `.mp4`, `.wav`, `.m4a` URLs (S3, GCS, public)"
            )
