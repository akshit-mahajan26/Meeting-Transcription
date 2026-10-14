"""Reusable presentational components."""

from __future__ import annotations

import re
from typing import Any

import streamlit as st
from utils.formatting import esc, format_timestamp

_AVATAR_COLORS = ["#6366F1", "#0EA5E9", "#10B981", "#F59E0B", "#EC4899", "#8B5CF6", "#14B8A6", "#EF4444"]


def speaker_color(name: str) -> str:
    """Stable colour per speaker name."""
    return _AVATAR_COLORS[sum(ord(c) for c in name) % len(_AVATAR_COLORS)]


def page_header(title: str, subtitle: str) -> None:
    st.markdown(
        f'<div class="page-title">{esc(title)}</div><div class="page-sub">{esc(subtitle)}</div>',
        unsafe_allow_html=True,
    )


def hero() -> None:
    st.markdown(
        """
        <div class="hero">
          <div class="hero-badge">Transcript intelligence</div>
          <h1 class="hero-title">Ask your meetings anything</h1>
          <div class="hero-sub">Query across ingested transcripts and get grounded answers with
          speaker attribution and verifiable source citations.</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def message_pills(msg: dict[str, Any]) -> str:
    mode = msg.get("routing_mode")
    pills = ""
    if mode == "MACRO":
        pills += '<span class="pill pill-macro">Macro</span>'
    elif mode == "MICRO":
        pills += '<span class="pill pill-micro">Micro</span>'
    elif mode == "LANGGRAPH_RAG":
        pills += '<span class="pill pill-macro">LangGraph RAG</span>'
    if msg.get("llm_model_used"):
        pills += f'<span class="pill pill-model">{esc(msg["llm_model_used"])}</span>'
    n = len(msg.get("citations") or [])
    if n:
        pills += f'<span class="pill pill-scope">{n} citation{"s" if n != 1 else ""}</span>'
    return f"<div>{pills}</div>" if pills else ""


def render_citations(citations: list[dict[str, Any]]) -> None:
    if not citations:
        return

    chips = '<div class="sources"><span class="sources-label">Sources</span>'
    for i, c in enumerate(citations, 1):
        spk = c.get("speaker") or "Unknown"
        t = format_timestamp(c.get("start_time"))
        chips += (
            f'<span class="chip" title="{esc(spk)} at {t}"><span class="chip-num">[{i}]</span>'
            f"{esc(spk)}<span class=\"chip-time\">{t}</span></span>"
        )
    st.markdown(chips + "</div>", unsafe_allow_html=True)

    with st.expander(f"Inspect source excerpts ({len(citations)})", icon=":material/format_quote:"):
        for i, c in enumerate(citations, 1):
            span = f"{format_timestamp(c.get('start_time'))} – {format_timestamp(c.get('end_time'))}"
            mid = c.get("meeting_id") or ""
            st.markdown(
                f"""
                <div class="cite">
                  <div class="cite-head">
                    <div><span class="chip-num">[{i}]</span>
                      <span class="cite-speaker">{esc(c.get("speaker") or "Unknown")}</span>
                      <span class="cite-ctx"> · {esc(c.get("topic_context") or "Level 1 turn")}</span></div>
                    <span class="cite-time">{span}</span>
                  </div>
                  <div class="cite-quote">“{esc(c.get("exact_raw_text"))}”</div>
                  <div class="cite-foot"><span>Meeting <code>{esc(mid[:16])}{"…" if len(mid) > 16 else ""}</code></span>
                    <span style="color:var(--good)">Verified provenance</span></div>
                </div>
                """,
                unsafe_allow_html=True,
            )


def render_assistant_message(msg: dict[str, Any]) -> None:
    pills = message_pills(msg)
    if pills:
        st.markdown(pills, unsafe_allow_html=True)
    if msg.get("routing_reasoning"):
        st.caption(f"DB scope: `{msg['routing_reasoning']}`", help="How the router scoped this query")
    st.markdown(msg.get("content", ""))
    render_citations(msg.get("citations") or [])


def highlight(text: str, needle: str) -> str:
    """Escape `text` and wrap case-insensitive matches of `needle` in <mark>."""
    safe = esc(text)
    if not needle:
        return safe
    return re.sub(f"({re.escape(esc(needle))})", r"<mark>\1</mark>", safe, flags=re.IGNORECASE)


def turn_card(turn: dict[str, Any], needle: str = "") -> None:
    spk = turn.get("speaker_name") or "Unknown"
    initials = "".join(w[0] for w in spk.split()[:2]).upper() or "?"
    st.markdown(
        f"""
        <div class="turn">
          <div class="avatar" style="background:{speaker_color(spk)}">{esc(initials)}</div>
          <div class="turn-body">
            <div class="turn-head"><span class="turn-spk">{highlight(spk, needle)}
              <span style="color:var(--faint);font-weight:400"> #{turn.get("turn_number") or 0}</span></span>
              <span class="turn-time">{format_timestamp(turn.get("start_time"))} – {format_timestamp(turn.get("end_time"))}</span></div>
            <div class="turn-text">{highlight(turn.get("text") or "", needle)}</div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
