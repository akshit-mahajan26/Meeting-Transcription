"""Meetings page: library on the left, transcript detail on the right."""

from __future__ import annotations

import pandas as pd
import streamlit as st
from ui.components import page_header, turn_card
from ui.nav import go
from ui.scope import set_scope
from ui.state import load_meeting, load_meetings
from utils.formatting import format_meeting_date, format_timestamp, speaker_talk_time, transcript_to_markdown

PAGE_SIZE = 50


def _detail(meeting_id: str) -> None:
    with st.spinner("Loading transcript…"):
        meeting = load_meeting(meeting_id)
    if not meeting:
        st.error("Could not load this meeting. Is the backend reachable?", icon=":material/error:")
        return

    turns = meeting.get("turns", [])
    talk = speaker_talk_time(turns)
    duration = max((float(t.get("end_time") or 0) for t in turns), default=0.0)

    h, a = st.columns([4, 2], vertical_alignment="center")
    h.markdown(f"### {meeting.get('title') or meeting_id}")
    if a.button("Ask about this meeting", icon=":material/chat:", type="primary", width="stretch"):
        set_scope(meeting_id=meeting_id)
        go("assistant")

    c0, c1, c2, c3 = st.columns(4)
    day, _, clock = format_meeting_date(meeting.get("date")).partition(", ")
    c0.metric(f"Held on · {clock}" if clock else "Held on", day, border=True)
    c1.metric("Turns", len(turns), border=True)
    c2.metric("Speakers", len(talk), border=True)
    c3.metric("Duration", format_timestamp(duration), border=True)

    summary = (meeting.get("summary") or "").strip()
    t_summary, t_turns, t_stats = st.tabs(
        [":material/summarize: Summary", ":material/forum: Transcript", ":material/bar_chart: Speakers"]
    )

    with t_summary:
        st.markdown(summary) if summary else st.caption("No summary available for this meeting.")

    with t_turns:
        f1, f2 = st.columns([3, 2])
        query = f1.text_input("Search", placeholder="Search text or speaker…", icon=":material/search:", label_visibility="collapsed").strip()
        speakers = f2.multiselect("Speakers", list(talk), placeholder="All speakers", label_visibility="collapsed")
        q = query.lower()
        shown = [
            t for t in turns
            if (not speakers or (t.get("speaker_name") or "Unknown") in speakers)
            and (not q or q in (t.get("text") or "").lower() or q in (t.get("speaker_name") or "").lower())
        ]
        cap, dl = st.columns([3, 2], vertical_alignment="center")
        cap.caption(f"Showing {len(shown)} of {len(turns)} turns")
        dl.download_button("Download transcript", transcript_to_markdown(meeting, turns), file_name=f"{meeting_id}.md",
                           mime="text/markdown", icon=":material/download:", width="stretch")

        key = f"limit_{meeting_id}_{q}_{'|'.join(speakers)}"
        limit = st.session_state.get(key, PAGE_SIZE)
        for t in shown[:limit]:
            turn_card(t, query)
        if len(shown) > limit and st.button(f"Show more ({len(shown) - limit} remaining)", key=f"more_{key}", width="stretch"):
            st.session_state[key] = limit + PAGE_SIZE
            st.rerun()

    with t_stats:
        if not talk:
            st.caption("No speaker data available.")
            return
        st.markdown("**Talk time (minutes)**")
        st.bar_chart(pd.DataFrame({"Speaker": list(talk), "Minutes": [round(v / 60, 2) for v in talk.values()]}),
                     x="Speaker", y="Minutes", horizontal=True)
        counts = pd.Series([t.get("speaker_name") or "Unknown" for t in turns]).value_counts()
        st.dataframe(counts.rename_axis("Speaker").reset_index(name="Turns"), hide_index=True, width="stretch",
                     column_config={"Turns": st.column_config.ProgressColumn("Turns", max_value=int(counts.max()), format="%d")})


def render() -> None:
    page_header("Meetings", "Browse ingested meetings and inspect their transcripts.")
    all_meetings = load_meetings()
    meetings = [m for m in all_meetings if m.get("status") != "FAILED"]
    failed = [m for m in all_meetings if m.get("status") == "FAILED"]
    if failed:
        with st.container(border=True):
            st.error(f"{len(failed)} ingestion{'s' if len(failed) != 1 else ''} failed. Submit the link again on the Ingest page to retry.",
                     icon=":material/error:")
            for m in failed:
                st.markdown(f"**{m.get('title') or m['id']}** · {m.get('project_id') or '—'}")
                st.caption(m.get("summary") or "No error details were recorded.")
    if not meetings:
        st.info("No meetings found. Ingest a meeting link to get started.", icon=":material/inbox:")
        return

    by_id = {m["id"]: m for m in meetings}
    projects = sorted({m.get("project_id") for m in meetings if m.get("project_id")})

    # A meeting opened from another page (Overview "Open") pre-fills the picker.
    arrived = st.session_state.pop("open_meeting_id", None)
    if arrived in by_id:
        st.session_state.meeting_pick = arrived
        st.session_state.meeting_project = "All projects"

    pick_col, proj_col = st.columns([4, 1.4], vertical_alignment="bottom")
    project = proj_col.selectbox("Project", ["All projects", *projects], key="meeting_project")
    options = [m["id"] for m in meetings if project == "All projects" or m.get("project_id") == project]
    if st.session_state.get("meeting_pick") not in options:
        st.session_state.pop("meeting_pick", None)

    def _label(mid: str) -> str:
        m = by_id[mid]
        return f"{m.get('title') or mid}  ·  {m.get('project_id') or '—'}  ·  {int(m.get('total_turns') or 0)} turns"

    meeting_id = pick_col.selectbox(
        "Meeting", options, index=None, format_func=_label, key="meeting_pick",
        placeholder=f"Search {len(options)} meeting{'s' if len(options) != 1 else ''} by title…",
    )

    if not meeting_id:
        with st.container(border=True):
            st.markdown("#### Select a meeting")
            st.caption("Search or choose a meeting above to see its summary, transcript and speaker insights.")
        return
    _detail(meeting_id)
