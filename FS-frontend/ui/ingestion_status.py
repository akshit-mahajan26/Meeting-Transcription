"""Ingestion status section: counts, filters, table and retry of failed or skipped meetings."""

from __future__ import annotations

from collections import Counter
from typing import Any

import pandas as pd
import streamlit as st
from services.meetings_api import retry_meeting
from ui.state import load_meetings, refresh_data
from utils.formatting import format_meeting_date

STATUS_LABELS = {
    "processed": "Processed", "processing": "Processing", "queued": "Queued",
    "retrying": "Retrying", "retries_stopped": "Retries stopped", "skipped": "Skipped",
}
RETRYABLE = {"retrying", "retries_stopped", "skipped"}
TILES = ["processed", "queued", "processing", "retrying", "retries_stopped", "skipped"]
SOURCES = {"link": "Link", "fireflies_sync": "Fireflies"}


def status_text(meeting: dict[str, Any]) -> str:
    s = meeting.get("ingestion_status")
    if s == "retrying":
        return f"Failed, retrying until {format_meeting_date(meeting.get('retry_until'))}"
    if s == "retries_stopped":
        return "Failed, retries stopped"
    if s == "skipped":
        return "Skipped, not available in Fireflies"
    return STATUS_LABELS.get(s or "", str(s or "—"))


def reason_text(meeting: dict[str, Any]) -> str:
    """Stored reason for failed or skipped meetings (also while queued/processing for a retry)."""
    if meeting.get("status") == "FAILED":
        return (meeting.get("summary") or "").strip()
    return ""


def count_by_status(meetings: list[dict[str, Any]]) -> Counter:
    return Counter(m.get("ingestion_status") for m in meetings)


def filter_rows(meetings: list[dict[str, Any]], status: str, source: str) -> list[dict[str, Any]]:
    """Filters by status label ('All' or a STATUS_LABELS value) and source label ('All', 'Link', 'Fireflies'),
    newest update first."""
    rows = [
        m for m in meetings
        if (status == "All" or STATUS_LABELS.get(m.get("ingestion_status") or "") == status)
        and (source == "All" or SOURCES.get(m.get("source") or "link", "Link") == source)
    ]
    return sorted(rows, key=lambda m: m.get("updated_at") or "", reverse=True)


def table_rows(meetings: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            "Title": m.get("title") or m.get("id", ""),
            "Source": SOURCES.get(m.get("source") or "link", "Link"),
            "Project": m.get("project_id") or "—",
            "Status": status_text(m),
            "Reason": reason_text(m),
            "Last updated": format_meeting_date(m.get("updated_at")),
        }
        for m in meetings
    ]


def retry_message(queued: int, processed: int, active: int, reconnect: int, too_soon: int, other: list[str]) -> str:
    lines = [f"{queued} queued for retry."]
    if processed:
        lines.append(f"{processed} already processed, not retried.")
    if active:
        lines.append(f"{active} already queued or processing.")
    if reconnect:
        lines.append(f"{reconnect} synced meetings need Fireflies reconnected before they can be retried.")
    if too_soon:
        lines.append(f"{too_soon} were retried less than a minute ago; try again shortly.")
    lines.extend(other)
    return " ".join(lines)


def retry_rows(rows: list[dict[str, Any]]) -> str:
    """Retries the retryable rows and composes the outcome message."""
    queued = processed = active = reconnect = too_soon = 0
    other: list[str] = []
    for m in rows:
        if m.get("ingestion_status") not in RETRYABLE:
            if m.get("ingestion_status") == "processed":
                processed += 1
            else:
                active += 1
            continue
        res = retry_meeting(m["id"])
        err = (res.get("error") or "").lower()
        if res.get("success"):
            queued += 1
        elif res.get("status_code") == 429 or "minute" in err:
            too_soon += 1
        elif "processed" in err:
            processed += 1
        elif "queued" in err:
            active += 1
        elif "reconnect" in err:
            reconnect += 1
        else:
            other.append(f"{m.get('title') or m['id']}: {res.get('error') or 'retry failed.'}")
    return retry_message(queued, processed, active, reconnect, too_soon, other)


def _body() -> None:
    meetings = load_meetings()
    counts = count_by_status(meetings)
    for col, key in zip(st.columns(len(TILES)), TILES, strict=True):
        col.metric(STATUS_LABELS[key], counts.get(key, 0), border=True)

    f1, f2, f3 = st.columns([2, 2, 1], vertical_alignment="bottom")
    status = f1.selectbox("Status", ["All", *STATUS_LABELS.values()], key="ing_status")
    source = f2.selectbox("Source", ["All", "Link", "Fireflies"], key="ing_source")
    if f3.button("Refresh", icon=":material/refresh:", key="ing_refresh", width="stretch"):
        refresh_data()
        st.rerun()

    rows = filter_rows(meetings, status, source)
    if not rows:
        st.caption("No meetings match these filters.")
        return
    event = st.dataframe(
        pd.DataFrame(table_rows(rows)), hide_index=True, width="stretch",
        on_select="rerun", selection_mode="multi-row", key="ing_table",
    )
    picked = [rows[i] for i in event.selection.rows if i < len(rows)]
    if st.button("Retry selected", icon=":material/replay:", key="ing_retry", disabled=not picked):
        st.session_state.ing_msg = retry_rows(picked)
        refresh_data()
        st.rerun()
    if msg := st.session_state.get("ing_msg"):
        st.info(msg, icon=":material/info:")


_body_live = st.fragment(run_every=5)(_body)


def render_ingestion_status(sync_running: bool = False) -> None:
    (_body_live if sync_running else _body)()
