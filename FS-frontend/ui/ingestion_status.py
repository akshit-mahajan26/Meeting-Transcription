"""Ingestion status section: counts, filters, table and retry of failed or skipped meetings."""

from __future__ import annotations

from collections import Counter
from typing import Any

import pandas as pd
import streamlit as st
from services.meetings_api import delete_meeting, retry_meeting
from ui.state import load_meetings, refresh_data
from utils.formatting import format_meeting_date

STATUS_LABELS = {
    "processed": "Processed", "processing": "Processing", "queued": "Queued",
    "retrying": "Retrying", "needs_attention": "Needs attention",
}
RETRYABLE = {"retrying", "retries_stopped", "skipped"}
TILES = ["processed", "queued", "processing", "retrying", "needs_attention"]
DELETE_KEY = "ing_del_n"  # counter only: bumping it gives the password widget a fresh, empty key
SOURCES = {"link": "Link", "fireflies_sync": "Fireflies"}


def group_of(meeting: dict[str, Any]) -> str:
    """Tile/filter group: failed meetings with retries open are 'retrying', other failed 'needs_attention'."""
    s = meeting.get("ingestion_status") or ""
    if s == "retrying":
        return "retrying"
    return "needs_attention" if s in RETRYABLE else s


def status_text(meeting: dict[str, Any]) -> str:
    s = meeting.get("ingestion_status")
    if s in RETRYABLE and meeting.get("status_label"):
        return meeting["status_label"]
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


def next_step_text(meeting: dict[str, Any]) -> str:
    if meeting.get("status") == "FAILED":
        return (meeting.get("next_step") or "").strip()
    return ""


def count_by_status(meetings: list[dict[str, Any]]) -> Counter:
    return Counter(group_of(m) for m in meetings)


def filter_rows(meetings: list[dict[str, Any]], status: str, source: str) -> list[dict[str, Any]]:
    """Filters by status label ('All' or a STATUS_LABELS value) and source label ('All', 'Link', 'Fireflies'),
    newest update first."""
    rows = [
        m for m in meetings
        if (status == "All" or STATUS_LABELS.get(group_of(m)) == status)
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
            "Next step": next_step_text(m),
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


def delete_message(deleted: int, failures: list[str]) -> str:
    """'{N} deleted.' plus the failed count and the first error message."""
    text = f"{deleted} deleted."
    if failures:
        text += f" {len(failures)} failed: {failures[0]}"
    return text


def delete_rows(rows: list[dict[str, Any]], password: str) -> tuple[int, list[str], bool]:
    """Deletes each row with one password. Returns (deleted, failure messages, wrong_password).
    Stops at the first 403 since the rest would fail the same way."""
    deleted = 0
    failures: list[str] = []
    for m in rows:
        res = delete_meeting(m["id"], password)
        if res.get("success"):
            deleted += 1
        elif res.get("status_code") == 403:
            return deleted, failures, True
        else:
            failures.append(res.get("error") or "Delete failed.")
    return deleted, failures, False


@st.dialog("Delete meetings")
def _delete_dialog(rows: list[dict[str, Any]]) -> None:
    st.write(f"Permanently delete the {len(rows)} selected meeting(s)? This cannot be undone.")
    n = st.session_state.setdefault(DELETE_KEY, 0)
    password = st.text_input("Your platform password", type="password", key=f"ing_del_pw_{n}")
    cancel_col, ok_col = st.columns(2)
    if cancel_col.button("Cancel", key="ing_del_cancel", width="stretch"):
        st.session_state.pop(f"ing_del_pw_{n}", None)
        st.session_state[DELETE_KEY] = n + 1
        st.rerun()
    if ok_col.button("Delete", type="primary", key="ing_del_ok", width="stretch"):
        if not password:
            st.error("Enter your platform password.")
            return
        deleted, failures, wrong = delete_rows(rows, password)
        st.session_state.pop(f"ing_del_pw_{n}", None)
        st.session_state[DELETE_KEY] = n + 1
        if deleted or failures:
            st.session_state.ing_msg = delete_message(deleted, failures)
            refresh_data()
        if wrong and not deleted and not failures:
            st.error("Incorrect password.")
            return
        if wrong:
            st.session_state.ing_msg += " Stopped: incorrect password."
        st.rerun()


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
        column_config={
            "Reason": st.column_config.TextColumn("Reason", width="large"),
            "Next step": st.column_config.TextColumn("Next step", width="large"),
        },
        on_select="rerun", selection_mode="multi-row", key="ing_table",
    )
    picked = [rows[i] for i in event.selection.rows if i < len(rows)]
    retry_col, delete_col, _ = st.columns([1, 1, 3])
    if retry_col.button("Retry selected", icon=":material/replay:", key="ing_retry", disabled=not picked):
        st.session_state.ing_msg = retry_rows(picked)
        refresh_data()
        st.rerun()
    if delete_col.button("Delete selected", icon=":material/delete:", key="ing_delete", disabled=not picked):
        _delete_dialog(picked)
    if msg := st.session_state.get("ing_msg"):
        st.info(msg, icon=":material/info:")


_body_live = st.fragment(run_every=5)(_body)


def render_ingestion_status(sync_running: bool = False) -> None:
    (_body_live if sync_running else _body)()
