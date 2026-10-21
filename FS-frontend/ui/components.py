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

    chips = '<div class="sources"><span class="sources-label">Sources:</span>'
    for i, c in enumerate(citations, 1):
        label = c.get("meeting_title") or "Meeting"
        spk = c.get("speaker") or "Unknown"
        start_t = format_timestamp(c.get("start_time"))
        end_t = format_timestamp(c.get("end_time"))
        span = f"{start_t} – {end_t}" if c.get("end_time") is not None else start_t
        quote = c.get("exact_raw_text") or c.get("topic_context") or "Referenced in transcript"

        popover_html = (
            f'<div class="chip-popover">'
            f'  <div class="popover-top">'
            f'    <span class="popover-badge">Source [{i}]</span>'
            f'    <span class="popover-time">⏱️ {span}</span>'
            f'  </div>'
            f'  <div class="popover-title">{esc(label)}</div>'
            f'  <div class="popover-speaker">👤 {esc(spk)}</div>'
            f'  <div class="popover-quote">“{esc(quote)}”</div>'
            f'  <div class="popover-foot"><span>✓ Verified provenance</span></div>'
            f'</div>'
        )

        chips += (
            f'<span class="chip">'
            f'<span class="chip-num">[{i}]</span>'
            f'<span class="chip-label">{esc(label)}</span>'
            f'{popover_html}'
            f'</span>'
        )
    st.markdown(chips + "</div>", unsafe_allow_html=True)
    _inject_popover_script()


def _inject_popover_script() -> None:
    """Injects a client-side boundary detector so popovers flip alignment if near container edge."""
    js = """
    <script>
    (function() {
      try {
        const rootDoc = (window.parent && window.parent.document) ? window.parent.document : document;
        // Replace any handler attached by an earlier version of this script (the page document outlives reruns)
        if (rootDoc.__popoverAlignHandler) rootDoc.removeEventListener('mouseover', rootDoc.__popoverAlignHandler);

        rootDoc.__popoverAlignHandler = function(e) {
          const chip = e.target.closest('.chip');
          if (!chip) return;
          const popover = chip.querySelector('.chip-popover');
          if (!popover) return;

          const chipRect = chip.getBoundingClientRect();
          // The message bubble is narrower than the chat container, so measure against the bubble first
          const chatBox = chip.closest('[data-testid="stChatMessage"]') || chip.closest('.st-key-chat_container') || rootDoc.body;
          const boxRect = chatBox.getBoundingClientRect();
          const rightEdge = Math.min(boxRect.right, (window.parent || window).innerWidth);

          // If the popover would run past the right edge, flip it so it ends at the chip's right edge
          if (rightEdge - chipRect.left < popover.offsetWidth + 8) {
            popover.classList.add('popover-align-right');
          } else {
            popover.classList.remove('popover-align-right');
          }
        };
        rootDoc.addEventListener('mouseover', rootDoc.__popoverAlignHandler);
      } catch(e) {}
    })();
    </script>
    """
    st.iframe(js, height=1)


def render_assistant_message(msg: dict[str, Any]) -> None:
    # pills = message_pills(msg)
    # if pills:
    #     st.markdown(pills, unsafe_allow_html=True)
    # if msg.get("routing_reasoning"):
    #     st.caption(f"DB scope: `{msg['routing_reasoning']}`", help="How the router scoped this query")
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
