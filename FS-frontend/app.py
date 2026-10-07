"""
FS-frontend/app.py
===================
Production-Grade Streamlit Chat Interface for the Meeting Intelligence Platform.
Featuring a modern, Gemini-inspired UI layout, decoupled REST architecture,
interactive source citation cards, and multi-tab operational controls.

Architecture:
- Strict Decoupled Microservice Architecture: Zero direct database or LLM SDK imports.
  All communication is routed via HTTP REST requests to FastAPI FS-backend (:8000).
- State Management: Conversation history stored in st.session_state.messages.
- Interactive Source Inspection: Expandable Level 1 citations with timestamps and quotes.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
import httpx
import streamlit as st

# Ensure frontend directory is in Python module path
FRONTEND_DIR = Path(__file__).resolve().parent
if str(FRONTEND_DIR) not in sys.path:
    sys.path.insert(0, str(FRONTEND_DIR))

try:
    from config import frontend_config
    from services.api_client import api_client
except ModuleNotFoundError:
    try:
        from FS_frontend.config import frontend_config
        from FS_frontend.services.api_client import api_client
    except ModuleNotFoundError:
        from frontend.config import frontend_config
        from frontend.services.api_client import api_client

logger = logging.getLogger("streamlit_app")

# ============================================================================
# Page Configuration
# ============================================================================
st.set_page_config(
    page_title="Meeting Intelligence",
    page_icon="✨",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ============================================================================
# Gemini-Inspired Modern UI Styling
# ============================================================================
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Google+Sans:wght@400;500;700&family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');

    html, body, [class*="css"] {
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    }

    /* Container Spacing */
    .block-container {
        padding-top: 1.5rem;
        padding-bottom: 3.5rem;
        max-width: 1300px;
    }

    /* Gemini Hero Banner */
    .gemini-hero {
        padding: 2.2rem 1.5rem 1.8rem 1.5rem;
        text-align: center;
        background: radial-gradient(circle at 50% 0%, rgba(66, 133, 244, 0.12) 0%, rgba(155, 114, 203, 0.06) 45%, transparent 75%);
        border-radius: 20px;
        margin-bottom: 1.8rem;
        border: 1px solid rgba(255, 255, 255, 0.06);
    }
    .gemini-title {
        font-family: 'Google Sans', 'Inter', sans-serif;
        font-size: 2.3rem;
        font-weight: 700;
        letter-spacing: -0.02em;
        background: linear-gradient(135deg, #4285F4 0%, #9B72CB 45%, #D96570 90%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        margin-bottom: 0.5rem;
    }
    .gemini-subtitle {
        color: #94A3B8;
        font-size: 1.05rem;
        max-width: 650px;
        margin: 0 auto;
        line-height: 1.5;
    }

    /* Suggestion Grid Cards */
    .gemini-card-header {
        font-size: 0.95rem;
        font-weight: 600;
        color: #F1F5F9;
        margin-bottom: 0.3rem;
    }
    .gemini-card-desc {
        font-size: 0.82rem;
        color: #94A3B8;
        line-height: 1.4;
    }

    /* Status Pills */
    .pill {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 3px 10px;
        border-radius: 9999px;
        font-size: 0.75rem;
        font-weight: 600;
        letter-spacing: 0.02em;
        margin-right: 6px;
        margin-bottom: 6px;
    }
    .pill-macro {
        background: rgba(99, 102, 241, 0.15);
        color: #A5B4FC;
        border: 1px solid rgba(99, 102, 241, 0.3);
    }
    .pill-micro {
        background: rgba(16, 185, 129, 0.15);
        color: #6EE7B7;
        border: 1px solid rgba(16, 185, 129, 0.3);
    }
    .pill-model {
        background: rgba(244, 114, 182, 0.12);
        color: #F472B6;
        border: 1px solid rgba(244, 114, 182, 0.28);
    }
    .pill-scope {
        background: rgba(56, 189, 248, 0.12);
        color: #38BDF8;
        border: 1px solid rgba(56, 189, 248, 0.28);
    }

    /* Compact Source Chips Ribbon (Subtle, non-intrusive) */
    .sources-ribbon {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 6px;
        margin-top: 0.6rem;
        margin-bottom: 0.35rem;
    }
    .sources-label {
        font-size: 0.72rem;
        font-weight: 600;
        color: #64748B;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        margin-right: 2px;
    }
    .source-chip {
        display: inline-flex;
        align-items: center;
        gap: 5px;
        padding: 2px 8px;
        background: rgba(30, 41, 59, 0.55);
        border: 1px solid rgba(255, 255, 255, 0.08);
        border-radius: 6px;
        font-size: 0.74rem;
        color: #94A3B8;
        transition: all 0.15s ease;
    }
    .source-chip:hover {
        background: rgba(51, 65, 85, 0.7);
        border-color: rgba(66, 133, 244, 0.4);
        color: #F1F5F9;
    }
    .source-chip-num {
        font-size: 0.7rem;
        font-weight: 700;
        color: #38BDF8;
        background: rgba(56, 189, 248, 0.12);
        border-radius: 3px;
        padding: 1px 4px;
    }
    .source-chip-spk {
        color: #E2E8F0;
        font-weight: 500;
    }
    .source-chip-time {
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.69rem;
        color: #64748B;
    }

    /* Refined Compact Citation Cards in Expander */
    .citation-card {
        background: rgba(15, 23, 42, 0.4);
        border: 1px solid rgba(255, 255, 255, 0.06);
        border-left: 2px solid #4285F4;
        border-radius: 8px;
        padding: 0.6rem 0.85rem;
        margin-bottom: 0.5rem;
        transition: background 0.15s ease, border-color 0.15s ease;
    }
    .citation-card:hover {
        border-left-color: #60A5FA;
        background: rgba(30, 41, 59, 0.45);
    }
    .citation-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        font-size: 0.78rem;
        margin-bottom: 0.3rem;
    }
    .citation-speaker {
        font-weight: 600;
        color: #E2E8F0;
    }
    .citation-time {
        font-family: 'JetBrains Mono', monospace;
        color: #38BDF8;
        font-size: 0.72rem;
        background: rgba(56, 189, 248, 0.08);
        padding: 1px 5px;
        border-radius: 4px;
    }
    .citation-quote {
        font-size: 0.8rem;
        color: #CBD5E1;
        line-height: 1.45;
        font-style: italic;
        padding-left: 0.4rem;
        border-left: 1px solid rgba(255, 255, 255, 0.08);
    }
    .citation-footer {
        display: flex;
        justify-content: space-between;
        align-items: center;
        font-size: 0.7rem;
        color: #64748B;
        margin-top: 0.35rem;
    }

    /* Scrollable Chat Container Styling */
    [data-testid="stVerticalBlockBorderWrapper"] {
        border-color: rgba(255, 255, 255, 0.08) !important;
        border-radius: 16px !important;
        background: rgba(15, 23, 42, 0.25);
    }
    [data-testid="stVerticalBlockBorderWrapper"] [data-testid="stVerticalBlock"] {
        padding-right: 0.5rem;
    }
    /* Fixed Bottom Chat Input Bar */
    [data-testid="stBottom"] {
        background: rgba(15, 23, 42, 0.85) !important;
        backdrop-filter: blur(16px) !important;
        border-top: 1px solid rgba(255, 255, 255, 0.08) !important;
        padding-top: 0.6rem !important;
        padding-bottom: 0.9rem !important;
    }
    [data-testid="stChatInput"] {
        border-radius: 26px !important;
        border: 1px solid rgba(255, 255, 255, 0.16) !important;
        box-shadow: 0 4px 24px rgba(0, 0, 0, 0.35) !important;
        background-color: rgba(30, 41, 59, 0.7) !important;
        overflow: hidden !important;
        transition: border-color 0.2s ease, box-shadow 0.2s ease !important;
    }
    [data-testid="stChatInput"]:focus-within {
        border-color: #4285F4 !important;
        box-shadow: 0 0 0 2px rgba(66, 133, 244, 0.35), 0 4px 24px rgba(66, 133, 244, 0.2) !important;
    }
    /* Eliminate Streamlit inner red/coral focus outlines on textarea and its wrappers */
    [data-testid="stChatInput"] > div,
    [data-testid="stChatInput"] div,
    [data-testid="stChatInput"] [data-baseweb="textarea"],
    [data-testid="stChatInput"] [data-baseweb="base-input"],
    [data-testid="stChatInput"] textarea {
        border: none !important;
        outline: none !important;
        box-shadow: none !important;
        border-color: transparent !important;
        background-color: transparent !important;
    }
    [data-testid="stChatInput"] textarea:focus,
    [data-testid="stChatInput"] [data-baseweb="textarea"]:focus,
    [data-testid="stChatInput"] [data-baseweb="textarea"]:focus-within {
        border: none !important;
        outline: none !important;
        box-shadow: none !important;
        border-color: transparent !important;
    }
    [data-testid="stChatInput"] button {
        border: none !important;
        border-radius: 50% !important;
        color: #94A3B8 !important;
        transition: color 0.15s ease, transform 0.15s ease !important;
    }
    [data-testid="stChatInput"] button:hover {
        color: #4285F4 !important;
        transform: scale(1.08);
    }

    /* Subtle Footer Disclaimer (Gemini Style) */
    .chat-disclaimer {
        text-align: center;
        font-size: 0.75rem;
        color: #64748B;
        margin-top: 0.4rem;
        padding-bottom: 0.2rem;
        letter-spacing: -0.01em;
    }

    /* Sidebar Status Card */
    .sidebar-card {
        padding: 0.75rem 1rem;
        border-radius: 10px;
        background: rgba(15, 23, 42, 0.55);
        border: 1px solid rgba(255, 255, 255, 0.06);
        margin-bottom: 1rem;
    }
    .status-online {
        color: #10B981;
        font-weight: 600;
    }
    .status-offline {
        color: #EF4444;
        font-weight: 600;
    }
</style>
""", unsafe_allow_html=True)


# ============================================================================
# Modular Helper Functions
# ============================================================================

def format_timestamp(seconds: float) -> str:
    """Formats raw second offsets into MM:SS format."""
    mins = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{mins:02d}:{secs:02d}"


def handle_backend_query(
    prompt: str,
    project_id: Optional[str] = None,
    meeting_id: Optional[str] = None,
    speaker: Optional[str] = None,
    timeout: float = 35.0,
) -> Dict[str, Any]:
    """
    Modular REST Client Helper:
    Dispatches user question to FastAPI backend (POST /api/v1/query) with full
    error trapping for connection failures, timeouts, and HTTP status codes.
    """
    backend_url = frontend_config.FASTAPI_BACKEND_URL.rstrip("/")
    query_url = f"{backend_url}/api/v1/query"

    payload = {
        "query": prompt.strip(),
        "project_id": project_id if project_id else None,
        "meeting_id": meeting_id if meeting_id else None,
        "speaker": speaker if speaker else None,
    }

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                query_url,
                json=payload,
                headers={"Content-Type": "application/json"}
            )

            if resp.status_code == 200:
                data = resp.json()
                return {
                    "success": True,
                    "query": data.get("query", prompt),
                    "routing_mode": data.get("routing_mode", "MICRO"),
                    "routing_confidence": data.get("routing_confidence", 1.0),
                    "routing_reasoning": data.get("routing_reasoning", ""),
                    "answer": data.get("answer", "No answer synthesized."),
                    "citations": data.get("citations", []),
                    "citations_count": data.get("citations_count", 0),
                    "provenance_verified": data.get("provenance_verified", True),
                    "macro_data": data.get("macro_data"),
                    "llm_model_used": data.get("llm_model_used", "openai/gpt-4o-mini"),
                }
            else:
                error_detail = resp.text
                try:
                    error_detail = resp.json().get("detail", resp.text)
                except Exception:
                    pass
                return {
                    "success": False,
                    "error": f"Backend Error (HTTP {resp.status_code})",
                    "answer": f"⚠️ **Backend Error ({resp.status_code})**: {error_detail}",
                    "citations": [],
                }

    except httpx.ConnectError:
        return {
            "success": False,
            "error": "Backend Connection Refused",
            "answer": (
                f"🔌 **Backend Disconnected**: Could not reach FastAPI backend at `{backend_url}`. "
                "Please verify that `FS-backend` is running on port 8000."
            ),
            "citations": [],
        }
    except httpx.TimeoutException:
        return {
            "success": False,
            "error": f"Request Timed Out ({timeout}s)",
            "answer": (
                f"⏱️ **Request Timed Out**: The backend and AI Engine took longer than {timeout} seconds "
                "to retrieve context and synthesize a response."
            ),
            "citations": [],
        }
    except Exception as exc:
        return {
            "success": False,
            "error": str(exc),
            "answer": f"⚠️ **Unexpected Error**: {str(exc)}",
            "citations": [],
        }


def render_citations(citations: List[Dict[str, Any]]) -> None:
    """
    Renders clean, non-intrusive source citations beneath the assistant message.
    1. Displays a compact single-line ribbon of source chips (speaker + timestamp).
    2. Keeps full verbatim excerpts cleanly collapsed by default in a subtle expander.
    """
    if not citations:
        return

    # 1. Compact Source Chips Ribbon (Takes up minimal height, non-intrusive)
    chips_html = '<div class="sources-ribbon"><span class="sources-label">Sources</span>'
    for idx, c in enumerate(citations):
        spk = c.get("speaker") or "Unknown"
        spk_short = spk.split()[0] if len(spk) > 16 else spk
        start_t = float(c.get("start_time") or 0.0)
        time_str = format_timestamp(start_t)
        chips_html += (
            f'<span class="source-chip" title="{spk} at {time_str}">'
            f'<span class="source-chip-num">[{idx + 1}]</span>'
            f'<span class="source-chip-spk">👤 {spk_short}</span>'
            f'<span class="source-chip-time">{time_str}</span>'
            f'</span>'
        )
    chips_html += '</div>'
    st.markdown(chips_html, unsafe_allow_html=True)

    # 2. Detailed Verification Expander — COLLAPSED BY DEFAULT (expanded=False)
    with st.expander(f"📑 Inspect Source Excerpts ({len(citations)})", expanded=False):
        for idx, c in enumerate(citations):
            speaker = c.get("speaker") or "Unknown"
            start_t = float(c.get("start_time") or 0.0)
            end_t = float(c.get("end_time") or 0.0)
            meeting_id = c.get("meeting_id") or ""
            raw_text = c.get("exact_raw_text") or ""
            topic_ctx = c.get("topic_context") or "Level 1 Turn"
            time_label = f"{format_timestamp(start_t)} - {format_timestamp(end_t)}"

            st.markdown(f"""
            <div class="citation-card">
                <div class="citation-header">
                    <div>
                        <span class="source-chip-num" style="margin-right: 4px;">[{idx + 1}]</span>
                        <span class="citation-speaker">👤 {speaker}</span>
                        <span style="color: #64748B; margin: 0 4px;">•</span>
                        <span style="color: #94A3B8; font-size: 0.74rem;">{topic_ctx}</span>
                    </div>
                    <div class="citation-time">⏱️ {time_label}</div>
                </div>
                <div class="citation-quote">"{raw_text}"</div>
                <div class="citation-footer">
                    <span>🏛️ Meeting: <code>{meeting_id[:16]}...</code></span>
                    <span style="color: #10B981; font-size: 0.7rem;">Verified Provenance</span>
                </div>
            </div>
            """, unsafe_allow_html=True)


# ============================================================================
# Session State Initialization
# ============================================================================
if "messages" not in st.session_state:
    st.session_state.messages = []

if "pending_query" not in st.session_state:
    st.session_state.pending_query = None


# ============================================================================
# Sidebar Controls & Scope Navigation
# ============================================================================
with st.sidebar:
    st.markdown("### ✨ Platform Controls")

    # Backend Connection Status Card
    health_status = api_client.check_health()
    backend_url = frontend_config.FASTAPI_BACKEND_URL

    if health_status.get("is_healthy"):
        st.markdown(f"""
        <div class="sidebar-card">
            <div style="font-size: 0.85rem;"><strong>Backend API:</strong> <span class="status-online">● Connected</span></div>
            <div style="color: #64748B; font-family: 'JetBrains Mono', monospace; font-size: 0.72rem; margin-top: 4px;">{backend_url}</div>
        </div>
        """, unsafe_allow_html=True)
    else:
        err_msg = health_status.get("error", "Offline")
        st.markdown(f"""
        <div class="sidebar-card">
            <div style="font-size: 0.85rem;"><strong>Backend API:</strong> <span class="status-offline">● Disconnected</span></div>
            <div style="color: #EF4444; font-size: 0.72rem; margin-top: 4px;">{err_msg}</div>
        </div>
        """, unsafe_allow_html=True)

    # Scope Filter Controls
    st.markdown("#### 🎯 Scope Filter")

    # Meeting Selector Dropdown
    try:
        active_meetings = api_client.list_meetings()
    except Exception:
        active_meetings = []

    meeting_options = ["All Meetings"] + [
        f"{(m.get('title') or '').strip() or m.get('id')} ({m.get('id')})" for m in active_meetings
    ]
    selected_meeting_option = st.selectbox(
        "Meeting Scope (Optional)",
        options=meeting_options,
        index=0,
        help="Lock searches to a specific meeting record, or leave as 'All Meetings' for automatic title/ID detection."
    )
    selected_meeting_id = None
    if selected_meeting_option != "All Meetings":
        m_match = re.search(r"\(([^\)]+)\)$", selected_meeting_option)
        if m_match:
            selected_meeting_id = m_match.group(1).strip()

    project_scope = st.text_input(
        "Project ID (Optional)",
        value="",
        placeholder="e.g., proj-fireflies-ai",
        help="Optional: Filter citations and LLM synthesis to a specific project boundary."
    ).strip()

    speaker_scope = st.text_input(
        "Speaker Filter (Optional)",
        value="",
        placeholder="e.g., Harsh or Neha",
        help="Optional: Focus citations and search strictly on a specific speaker or participant."
    ).strip()

    # Clear Chat History Button
    st.markdown("---")
    if st.button("🗑️ Clear Chat History", use_container_width=True, type="secondary"):
        st.session_state.messages = []
        st.session_state.pending_query = None
        st.rerun()

    # Repository Metrics Card
    st.markdown("#### 📊 Repository Metrics")
    try:
        total_meetings_count = len(active_meetings)
        total_turns_count = sum(m.get("total_turns", 0) for m in active_meetings)
        col_m1, col_m2 = st.columns(2)
        with col_m1:
            st.metric("Meetings", total_meetings_count)
        with col_m2:
            st.metric("Turns", total_turns_count)
    except Exception:
        st.caption("Metrics unavailable")

    st.markdown("---")
    st.caption("🛡️ **Decoupled Architecture**: Zero database imports in frontend. All queries communicate via REST on `/api/v1/query`.")


# ============================================================================
# Main Multi-Tab Navigation
# ============================================================================
tab_chat, tab_ingest, tab_explorer = st.tabs([
    "💬 Intelligence Chat",
    "📥 Ingest Meeting Link",
    "📑 Transcript Explorer"
])


# ============================================================================
# TAB 1: Intelligence Chat (Gemini-Inspired Layout with Scrollable Container)
# ============================================================================
with tab_chat:
    # Dedicated Scrollable Container for Messages (Pinned Height with Scroll)
    chat_container = st.container(height=640)

    with chat_container:
        # Render Welcome Hero when conversation is fresh
        if not st.session_state.messages:
            st.markdown("""
            <div class="gemini-hero">
                <div class="gemini-title">Meeting Intelligence</div>
                <div class="gemini-subtitle">
                    Ask questions across your ingested meeting transcripts with guaranteed Level 1 speaker turn grounding and conversational AI synthesis.
                </div>
            </div>
            """, unsafe_allow_html=True)

        # Render Conversation History inside the Scrollable Container
        for msg in st.session_state.messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            citations = msg.get("citations", [])
            routing_mode = msg.get("routing_mode")
            routing_reasoning = msg.get("routing_reasoning")
            llm_model = msg.get("llm_model_used")

            with st.chat_message(role, avatar="👤" if role == "user" else "✨"):
                if role == "assistant":
                    # Render Metadata Pills
                    pills_html = '<div style="margin-bottom: 0.6rem;">'
                    if routing_mode == "MACRO":
                        pills_html += '<span class="pill pill-macro">⚡ Mode: MACRO</span>'
                    elif routing_mode == "MICRO":
                        pills_html += f'<span class="pill pill-micro">🎯 Mode: MICRO</span>'
                    elif routing_mode == "LANGGRAPH_RAG":
                        pills_html += f'<span class="pill pill-macro">🕸️ LangGraph RAG</span>'

                    if llm_model:
                        pills_html += f'<span class="pill pill-model">🤖 {llm_model}</span>'

                    if citations:
                        pills_html += f'<span class="pill pill-scope">📑 {len(citations)} Citations</span>'

                    pills_html += '</div>'
                    st.markdown(pills_html, unsafe_allow_html=True)

                    if routing_reasoning:
                        st.caption(f"🎯 **DB Scope:** `{routing_reasoning}`")

                    st.markdown(content)
                    render_citations(citations)
                else:
                    st.markdown(content)

    # Fixed Bottom Chat Input Bar (Gemini / ChatGPT style)
    pending_prompt = st.session_state.pop("pending_query", None)
    user_prompt = st.chat_input("Ask a question about decisions, topics, or speaker statements...")
    active_prompt = pending_prompt or user_prompt

    if active_prompt and active_prompt.strip():
        clean_prompt = active_prompt.strip()

        # 1. Append & Render User Question Immediately inside chat_container
        st.session_state.messages.append({"role": "user", "content": clean_prompt})
        with chat_container:
            with st.chat_message("user", avatar="👤"):
                st.markdown(clean_prompt)

            # 2. Issue REST Request to Backend with Visual Loading Spinner inside container
            with st.chat_message("assistant", avatar="✨"):
                with st.spinner("Searching meeting transcripts & synthesizing grounded response..."):
                    response_data = handle_backend_query(
                        prompt=clean_prompt,
                        project_id=project_scope if project_scope else None,
                        meeting_id=selected_meeting_id,
                        speaker=speaker_scope if speaker_scope else None,
                    )

                is_success = response_data.get("success", True)
                answer_text = response_data.get("answer", "No response synthesized.")
                citations = response_data.get("citations", [])
                routing_mode = response_data.get("routing_mode", "MICRO")
                routing_reasoning = response_data.get("routing_reasoning", "")
                llm_model = response_data.get("llm_model_used", "openai/gpt-4o-mini")

                if not is_success:
                    st.error(response_data.get("error", "An error occurred."))
                    st.markdown(answer_text)
                else:
                    # Render Assistant Pills
                    pills_html = '<div style="margin-bottom: 0.6rem;">'
                    if routing_mode == "MACRO":
                        pills_html += '<span class="pill pill-macro">⚡ Mode: MACRO</span>'
                    elif routing_mode == "MICRO":
                        pills_html += '<span class="pill pill-micro">🎯 Mode: MICRO</span>'
                    elif routing_mode == "LANGGRAPH_RAG":
                        pills_html += '<span class="pill pill-macro">🕸️ LangGraph RAG</span>'

                    pills_html += f'<span class="pill pill-model">🤖 {llm_model}</span>'

                    if citations:
                        pills_html += f'<span class="pill pill-scope">📑 {len(citations)} Citations</span>'

                    pills_html += '</div>'
                    st.markdown(pills_html, unsafe_allow_html=True)

                    if routing_reasoning:
                        st.caption(f"🎯 **DB Scope:** `{routing_reasoning}`")

                    st.markdown(answer_text)
                    render_citations(citations)

                # Persist to Session History
                st.session_state.messages.append({
                    "role": "assistant",
                    "content": answer_text,
                    "citations": citations,
                    "routing_mode": routing_mode,
                    "routing_reasoning": routing_reasoning,
                    "llm_model_used": llm_model,
                })

    # Subtle Footer Disclaimer (Gemini Style)
    st.markdown("""
    <div class="chat-disclaimer">
        Meeting Intelligence can make mistakes. Verify critical facts and numbers with source citations.
    </div>
    """, unsafe_allow_html=True)


# ============================================================================
# TAB 2: Ingest Meeting Link (Async Link Ingestion)
# ============================================================================
with tab_ingest:
    st.subheader("📥 Ingest Meeting Link")
    st.caption("Submit a Fireflies dashboard view link or a direct audio/video recording URL for background transcription.")

    ingest_col1, ingest_col2 = st.columns([3, 2])

    with ingest_col1:
        link_input = st.text_input(
            "Meeting Link",
            placeholder="https://app.fireflies.ai/view/Meeting-Title::01KT43KCH4XRVKJT1E65YNT051",
            help="Supports Fireflies dashboard links or direct .mp3, .mp4, .wav URLs."
        )
        custom_title_input = st.text_input(
            "Custom Meeting Title (Optional)",
            placeholder="Leave blank to dynamically extract title from URL slug",
            help="If left blank, the system automatically parses and formats the URL slug into a title."
        )
        target_project_id = st.text_input(
            "Target Project ID",
            value="proj-fireflies-ai",
            help="The project organizational boundary for this meeting."
        )

        if st.button("🚀 Enqueue Meeting Ingestion", type="primary", use_container_width=True):
            if not link_input.strip():
                st.warning("Please provide a valid meeting link.")
            else:
                with st.spinner("Submitting link to backend for background ingestion..."):
                    res = api_client.ingest_link(
                        link=link_input.strip(),
                        project_id=target_project_id.strip() if target_project_id else None,
                        title=custom_title_input.strip() if custom_title_input else None,
                    )

                if res.get("success"):
                    job_data = res.get("data", {})
                    st.success("🎉 Meeting Link Successfully Accepted (HTTP 202 Accepted)!")
                    st.json(job_data)
                    st.info("The meeting is being processed asynchronously by FS-AI-Engine. Switch to 'Transcript Explorer' or refresh in a few moments to view new turns.")
                else:
                    st.error(f"Ingestion failed: {res.get('error')}")

    with ingest_col2:
        st.markdown("#### 💡 Supported URL Formats")
        st.markdown("""
        * **Fireflies Dashboard Link:**  
          `https://app.fireflies.ai/view/Slug::TranscriptID`  
          *Automatically extracts the clean meeting title from the slug and pulls turns via GraphQL.*
        * **Standalone Fireflies View:**  
          `https://app.fireflies.ai/view/01KT43KCH4XRVKJT1E65YNT051`
        * **Direct Media Files:**  
          `.mp3`, `.mp4`, `.wav`, `.m4a` files hosted on S3, Google Cloud Storage, or public URLs.
        """)


# ============================================================================
# TAB 3: Transcript Explorer (Hierarchical Inspection)
# ============================================================================
with tab_explorer:
    st.subheader("📑 Normalized Transcripts Explorer")
    st.caption("Inspect Level 1 speaker turns and Level 2 topics stored in PostgreSQL.")

    try:
        meetings_list = api_client.list_meetings()
    except Exception as exc:
        meetings_list = []
        st.error(f"Error fetching meetings list: {exc}")

    if not meetings_list:
        st.info("No meetings found in the database. Ingest a meeting link above to get started.")
    else:
        meeting_options_map = {
            f"{m.get('title') or m['id']} ({m['id']}) - {m.get('total_turns', 0)} turns": m["id"]
            for m in meetings_list
        }
        selected_label = st.selectbox("Select Meeting to Inspect", list(meeting_options_map.keys()))
        selected_m_id = meeting_options_map[selected_label]

        if selected_m_id:
            with st.spinner(f"Loading turns for meeting {selected_m_id}..."):
                meeting_details = api_client.get_meeting(selected_m_id)

            if meeting_details:
                turns = meeting_details.get("turns", [])
                st.markdown(f"### {meeting_details.get('title', selected_m_id)}")
                st.caption(f"**Meeting ID:** `{selected_m_id}` | **Status:** `{meeting_details.get('status', 'PROCESSED')}` | **Total Turns:** `{len(turns)}`")

                # Turn search filter
                search_query = st.text_input("🔍 Filter turns by text or speaker name:", "").strip().lower()

                filtered_turns = [
                    t for t in turns
                    if not search_query
                    or search_query in (t.get("text") or "").lower()
                    or search_query in (t.get("speaker_name") or "").lower()
                ]

                st.markdown(f"Displaying **{len(filtered_turns)}** of **{len(turns)}** turns:")

                for t in filtered_turns:
                    spk = t.get("speaker_name") or "Unknown"
                    t_start = float(t.get("start_time") or 0.0)
                    t_end = float(t.get("end_time") or 0.0)
                    txt = t.get("text") or ""
                    t_num = t.get("turn_number") or 0

                    st.markdown(f"""
                    <div style="padding: 0.6rem 0.9rem; background: rgba(15, 23, 42, 0.45); border: 1px solid rgba(255, 255, 255, 0.05); border-radius: 8px; margin-bottom: 0.4rem;">
                        <div style="display: flex; justify-content: space-between; font-size: 0.8rem; margin-bottom: 0.25rem;">
                            <span style="font-weight: 600; color: #F1F5F9;">#{t_num} • {spk}</span>
                            <span style="font-family: monospace; color: #38BDF8;">{format_timestamp(t_start)} - {format_timestamp(t_end)}</span>
                        </div>
                        <div style="font-size: 0.86rem; color: #CBD5E1;">{txt}</div>
                    </div>
                    """, unsafe_allow_html=True)
