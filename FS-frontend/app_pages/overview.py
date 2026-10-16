"""Overview page: KPIs, project breakdown, recent meetings and a quick-ask box."""

from __future__ import annotations

import pandas as pd
import streamlit as st
from ui.components import page_header
from ui.nav import go
from ui.scope import set_scope
from ui.state import known_projects, load_meetings


def render() -> None:
    all_meetings = load_meetings()
    meetings = [m for m in all_meetings if m.get("status") != "FAILED"]
    projects = known_projects(meetings)
    page_header("Overview", "Your meeting knowledge base at a glance.")
    if failed := len(all_meetings) - len(meetings):
        st.warning(f"{failed} meeting ingestion{'s' if failed != 1 else ''} failed. See the **Meetings** page for details.",
                   icon=":material/error:")

    # Quick ask
    with st.form("quick_ask", border=False):
        c1, c2 = st.columns([6, 1], vertical_alignment="bottom")
        q = c1.text_input("Ask", placeholder="Ask anything across all your meetings…", label_visibility="collapsed", icon=":material/auto_awesome:")
        ask_submitted = c2.form_submit_button("Ask", type="primary", width="stretch", icon=":material/arrow_upward:")
    if ask_submitted and q.strip():
        st.session_state.pending_query = q.strip()
        go("assistant")

    total_turns = sum(int(m.get("total_turns") or 0) for m in meetings)
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Meetings", len(meetings), border=True)
    k2.metric("Projects", len(projects), border=True)
    k3.metric("Speaker turns", f"{total_turns:,}", border=True)
    k4.metric("Avg. turns / meeting", f"{total_turns // len(meetings):,}" if meetings else "—", border=True)

    if not meetings:
        st.info("No meetings yet. Head to **Ingest** to add your first recording.", icon=":material/inbox:")
        if st.button("Ingest a meeting", icon=":material/cloud_upload:", type="primary"):
            go("ingest")
        return

    left, right = st.columns([2, 3], gap="large")
    with left:
        with st.container(border=True):
            st.markdown("**Meetings by project**")
            df = pd.DataFrame(meetings)
            df["project_id"] = df.get("project_id", pd.Series(dtype=str)).fillna("Unassigned").replace("", "Unassigned")
            counts = df.groupby("project_id").size().rename("Meetings").reset_index().rename(columns={"project_id": "Project"})
            st.bar_chart(counts, x="Project", y="Meetings", horizontal=True, height=260)

    with right:
        with st.container(border=True):
            st.markdown("**Recent meetings**")
            for m in meetings[:6]:
                a, b, c = st.columns([5, 2, 2], vertical_alignment="center")
                a.markdown(f"**{(m.get('title') or m['id'])}**  \n<span style='color:var(--faint);font-size:.75rem'>{m.get('project_id') or 'Unassigned'} · {m.get('total_turns', 0)} turns</span>", unsafe_allow_html=True)
                if b.button("Open", key=f"open_{m['id']}", icon=":material/open_in_new:", width="stretch"):
                    st.session_state.open_meeting_id = m["id"]
                    go("meetings")
                if c.button("Ask", key=f"ask_{m['id']}", icon=":material/chat:", width="stretch"):
                    set_scope(meeting_id=m["id"])
                    go("assistant")
