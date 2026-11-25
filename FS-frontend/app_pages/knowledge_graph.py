"""Knowledge graph page: people, projects, decisions, action items and blockers linked across meetings."""

from __future__ import annotations

import streamlit as st
from services.api_client import api_client
from ui.components import page_header
from ui.graph_view import ENTITY_COLORS, ENTITY_LABELS, graph_view
from ui.nav import go
from ui.state import known_projects, load_graph, load_graph_entity, load_meetings, refresh_data
from utils.formatting import esc, format_timestamp, meeting_label

VIEW_KEY = "kg_view"


def _on_select() -> None:
    picked = (st.session_state.get(VIEW_KEY) or {}).get("selected")
    if picked:
        st.session_state.kg_selected = picked


def _legend(types: list[str]) -> None:
    chips = "".join(
        f'<span style="display:inline-flex;align-items:center;gap:6px;margin:0 14px 4px 0;font-size:0.82rem;">'
        f'<span style="width:10px;height:10px;border-radius:50%;background:{ENTITY_COLORS[t]};"></span>{esc(ENTITY_LABELS[t])}</span>'
        for t in types
    )
    st.markdown(f"<div>{chips}</div>", unsafe_allow_html=True)


def _coverage(stats: dict) -> None:
    k1, k2, k3 = st.columns(3)
    k1.metric("Entities", f"{stats.get('entities', 0):,}", border=True)
    k2.metric("Relationships", f"{stats.get('relations', 0):,}", border=True)
    k3.metric("Meetings in graph", f"{stats.get('meetings_in_graph', 0)} / {stats.get('meetings_total', 0)}", border=True)

    missing = stats.get("meetings_missing") or []
    if missing:
        c1, c2 = st.columns([4, 1], vertical_alignment="center")
        c1.info(
            f"{len(missing)} meeting{'s are' if len(missing) != 1 else ' is'} not in the graph yet "
            "(ingested before the graph existed, or the extraction failed).",
            icon=":material/hub:",
        )
        if c2.button("Build graph", icon=":material/auto_awesome:", type="primary", width="stretch"):
            res = api_client.rebuild_graph()
            if res.get("success"):
                st.toast(f"Building the graph of {res['data'].get('queued', 0)} meeting(s) in the background. Refresh in a minute.",
                         icon=":material/hourglass_top:")
            else:
                st.error(res.get("error") or "Could not start the graph build.", icon=":material/error:")


def _detail(entity_id: str) -> None:
    res = load_graph_entity(entity_id)
    if not res.get("success"):
        st.caption("Could not load this entity.")
        return
    e = res["data"]
    color = ENTITY_COLORS.get(e["type"], "#94A3B8")
    st.markdown(
        f'<div style="font-size:0.75rem;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:{color};">'
        f'{esc(e["type"].replace("_", " "))}</div><div style="font-size:1.15rem;font-weight:600;">{esc(e["label"])}</div>',
        unsafe_allow_html=True,
    )
    if e.get("description"):
        st.caption(e["description"])
    if e.get("aliases"):
        st.caption("Also called: " + ", ".join(e["aliases"]))

    if st.button("Ask about this", icon=":material/chat:", width="stretch"):
        st.session_state.pending_query = f"What do my meetings say about {e['label']}?"
        go("assistant")

    connections = e.get("connections") or []
    st.markdown(f"**Connections** · {len(connections)}")
    if not connections:
        st.caption("No connections yet.")
    for c in connections[:25]:
        other = c["entity"]
        arrow = f"{c['relation_type'].replace('_', ' ')} →" if c["direction"] == "out" else f"← {c['relation_type'].replace('_', ' ')}"
        dot = ENTITY_COLORS.get(other["type"], "#94A3B8")
        st.markdown(
            f'<div style="font-size:0.86rem;margin-bottom:6px;"><span style="opacity:.65">{esc(arrow)}</span> '
            f'<span style="color:{dot};">●</span> {esc(other["label"])}'
            f'<div style="font-size:0.75rem;opacity:.6">{esc(c.get("meeting_title") or "")}</div></div>',
            unsafe_allow_html=True,
        )

    meetings = e.get("meetings") or []
    with st.expander(f"Meetings · {len(meetings)}", icon=":material/video_library:"):
        for m in meetings:
            st.markdown(f"- {m.get('meeting_title') or m['meeting_id']}")

    evidence = e.get("evidence") or []
    with st.expander(f"Evidence · {len(evidence)}", icon=":material/format_quote:", expanded=bool(evidence)):
        for q in evidence:
            st.markdown(
                f'<div style="font-size:0.84rem;margin-bottom:10px;"><b>{esc(q["speaker"])}</b> '
                f'<span style="opacity:.6">· {esc(q.get("meeting_title") or "")} · {format_timestamp(q.get("start_time") or 0)}</span>'
                f'<div>“{esc(q["text"])}”</div></div>',
                unsafe_allow_html=True,
            )


def render() -> None:
    page_header("Knowledge graph", "People, projects, decisions, action items and blockers, linked across your meetings.")

    stats_res = api_client.get_graph_stats()
    if not stats_res.get("success"):
        if stats_res.get("status_code") == 503:
            st.warning("The knowledge graph is not configured on the backend (KG_DATABASE_URL).", icon=":material/hub:")
        else:
            st.error(stats_res.get("error") or "Could not reach the knowledge graph.", icon=":material/error:")
        return
    _coverage(stats_res["data"])

    meetings = [m for m in load_meetings() if m.get("status") != "FAILED"]
    projects = known_projects(meetings)
    all_types = list(ENTITY_COLORS)

    f1, f2, f3 = st.columns([2, 3, 1])
    project_id = f1.selectbox("Project", [None, *projects], format_func=lambda p: "All projects" if p is None else p)
    scoped = [m for m in meetings if not project_id or m.get("project_id") == project_id]
    ids = [None] + [m["id"] for m in scoped]
    labels = {m["id"]: meeting_label(m) for m in scoped}
    meeting_id = f2.selectbox("Meeting", ids, format_func=lambda i: "All meetings" if i is None else labels[i])
    limit = f3.number_input("Max nodes", min_value=20, max_value=500, value=150, step=10)
    types = st.pills(
        "Entity types", all_types, selection_mode="multi", default=all_types,
        format_func=lambda t: ENTITY_LABELS[t], label_visibility="collapsed",
    ) or all_types

    graph_res = load_graph(project_id, meeting_id, types, int(limit))
    if not graph_res.get("success"):
        st.error(graph_res.get("error") or "Could not load the graph.", icon=":material/error:")
        return
    graph = graph_res["data"]
    nodes, edges = graph.get("nodes") or [], graph.get("edges") or []
    if not nodes:
        st.info("Nothing in the graph for this selection yet.", icon=":material/hub:")
        return

    node_ids = {n["id"] for n in nodes}
    selected = st.session_state.get("kg_selected")
    if selected not in node_ids:
        selected = None

    left, right = st.columns([3, 1.25], gap="medium")
    with left:
        _legend(sorted({n["type"] for n in nodes}, key=all_types.index))
        graph_view(nodes, edges, selected=selected, key=VIEW_KEY, on_select=_on_select)
        st.caption(f"{len(nodes)} entities · {len(edges)} relationships")
    with right, st.container(border=True):
        if selected:
            _detail(selected)
        else:
            st.caption("Click a node to see its connections, the meetings it appears in, and the quotes behind it.")

    if st.button("Refresh", icon=":material/refresh:", type="tertiary"):
        refresh_data()
        st.rerun()
