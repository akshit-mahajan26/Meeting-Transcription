"""Ingest page, Section 1: Fireflies account (plan 14.2)."""

from __future__ import annotations

from typing import Any

import streamlit as st
from services import fireflies_api
from ui.state import known_projects, load_meetings, register_project

NO_PROJECT = "No project"
ADD_NEW = "Add new project…"
KEY_URL = "https://app.fireflies.ai/integrations/custom/fireflies"
SYNC_CAPTION = (
    "New meetings from Fireflies go into this project. Meetings already stored keep their project; "
    "change those on the Meetings page."
)
RUNNING_KEY = "ff_sync_running"
FORM_KEY = "ff_form_n"  # counter only: bumping it gives the secret widgets fresh keys, so they come back empty


def fmt_dt(value: Any) -> str:
    """ISO timestamp -> 'YYYY-MM-DD HH:MM UTC'."""
    text = str(value or "")
    return f"{text[:10]} {text[11:16]} UTC" if len(text) >= 16 else text


def fmt_date(value: Any) -> str:
    return str(value or "")[:10]


def status_lines(status: dict[str, Any]) -> list[str]:
    """Status line texts for the linked state (plan 14.2)."""
    if status.get("sync_running"):
        return [f"Syncing… last activity {fmt_dt(status.get('sync_heartbeat_at'))}"]
    result = status.get("last_sync_result") or {}
    if not result:
        return ["Not synced yet."]
    outcome = result.get("outcome")
    lines: list[str] = []
    when = fmt_dt(result.get("finished_at"))
    counts = (
        f"{result.get('new_or_updated', 0)} queued for ingestion, {result.get('unchanged', 0)} unchanged, "
        f"{result.get('skipped_other_account', 0)} skipped because another account owns the meeting"
    )
    if outcome == "completed":
        lines.append(f"Last synced {when}: {counts}.")
    elif outcome == "stopped_meeting_cap":
        n = result.get("new_or_updated", 0)
        lines.append(f"Stopped after {n} meetings (server limit). The next sync continues.")
    elif outcome == "stopped_account_error":
        lines.append(f"{result.get('error') or 'Fireflies reported a problem with your account.'} Auto-sync was turned off.")
    else:
        lines.append(f"Last sync {when} stopped: {result.get('error') or outcome or 'unknown error'}")
    removed = int(result.get("removed_from_fireflies") or 0)
    if result.get("full_sync") and removed > 0:
        purge = fmt_date((status.get("pending_deletion") or {}).get("purge_at"))
        lines.append(f"{removed} meetings are no longer in your Fireflies account and will be deleted on {purge}.")
    return lines


def _form_n() -> int:
    return st.session_state.setdefault(FORM_KEY, 0)


def _reset_secret_fields() -> None:
    n = _form_n()
    for name in ("ff_api_key", "ff_password", "ff_dlg_password"):
        st.session_state.pop(f"{name}_{n}", None)
    st.session_state[FORM_KEY] = n + 1


def _project_picker(label: str, key: str, projects: list[str], current: str | None) -> str | None:
    """Returns the chosen project name ('' for none), or None while 'Add new' has no name yet."""
    options = [NO_PROJECT, *projects, ADD_NEW]
    index = options.index(current) if current in options else 0
    choice = st.selectbox(label, options, index=index, key=key)
    if choice == ADD_NEW:
        name = st.text_input("New project name", key=f"{key}_new").strip()
        return name or None
    return "" if choice == NO_PROJECT else choice


def _error(res: dict[str, Any]) -> None:
    st.error(res.get("error") or "Something went wrong.")


def _render_not_linked(projects: list[str]) -> None:
    n = _form_n()
    st.link_button("Get my API key", KEY_URL, icon=":material/key:")
    st.caption("Log in to Fireflies, copy the API key on that page, paste it here.")
    project = _project_picker("Sync project (optional)", "ff_conn_project", projects, None)
    st.caption(
        "All meetings synced from this Fireflies account go into this project. "
        "Leave as No project to sort them later on the Meetings page."
    )
    api_key = st.text_input("Fireflies API key", type="password", key=f"ff_api_key_{n}")
    password = st.text_input("Your platform password", type="password", key=f"ff_password_{n}")
    if st.button("Connect", type="primary", icon=":material/link:"):
        if not api_key.strip() or not password:
            st.warning("Enter your Fireflies API key and your platform password.")
            return
        res = fireflies_api.fireflies_connect(password, api_key.strip(), project or None)
        _reset_secret_fields()
        if res.get("success"):
            if project:
                register_project(project)
            st.rerun()
        elif res.get("status_code") == 403:
            st.error("Incorrect password.")
        else:
            _error(res)


@st.dialog("Disconnect Fireflies")
def _disconnect_dialog(meetings: int, purge_date: str) -> None:
    st.write(
        "Disconnecting removes your Fireflies API key from this platform now. "
        f"The {meetings} meetings synced from Fireflies will be permanently deleted on {purge_date} (7 days from now). "
        "Meetings you added by link are not affected. Reconnecting the same Fireflies account before then keeps them. "
        "Your key still works at Fireflies: to revoke it, regenerate it in Fireflies (Integrations → Fireflies API)."
    )
    n = _form_n()
    password = st.text_input("Your platform password", type="password", key=f"ff_dlg_password_{n}")
    cancel_col, ok_col = st.columns(2)
    if cancel_col.button("Cancel", width="stretch"):
        _reset_secret_fields()
        st.rerun()
    if ok_col.button("Disconnect", type="primary", width="stretch"):
        res = fireflies_api.fireflies_disconnect(password)
        _reset_secret_fields()
        if res.get("success"):
            st.rerun()
        elif res.get("status_code") == 403:
            st.error("Incorrect password.")
        else:
            _error(res)


def _render_linked(status: dict[str, Any], projects: list[str]) -> None:
    from datetime import UTC, datetime, timedelta

    running = bool(status.get("sync_running"))
    st.markdown(f"**Linked account:** {status.get('account_email') or ''}".replace("<", "&lt;"))
    current = status.get("sync_project") or None
    shown = [p for p in projects if p != current] if current else projects
    picker_col, save_col = st.columns([4, 1], vertical_alignment="bottom")
    with picker_col:
        project = _project_picker("Sync project", "ff_sync_project", ([current] if current else []) + shown, current)
    with save_col:
        if st.button("Save", key="ff_save_project", disabled=project is None):
            res = fireflies_api.fireflies_update_settings(sync_project=project)
            if res.get("success"):
                if project:
                    register_project(project)
                st.rerun()
            else:
                _error(res)
    st.caption(SYNC_CAPTION)

    auto = st.toggle("Auto-sync", value=bool(status.get("auto_sync")), key="ff_auto_sync")
    if auto != bool(status.get("auto_sync")):
        res = fireflies_api.fireflies_update_settings(auto_sync=auto)
        if res.get("success"):
            st.rerun()
        _error(res)

    if st.button("Sync now", disabled=running, icon=":material/sync:"):
        res = fireflies_api.fireflies_sync()
        if res.get("success"):
            st.rerun()
        else:
            _error(res)

    for line in status_lines(status):
        st.caption(line)
    skipped = (status.get("last_sync_result") or {}).get("skipped_other_account_meetings") or []
    if skipped:
        with st.expander(f"{len(skipped)} meetings skipped: already in another account on this platform"):
            for item in skipped:
                st.text(item.get("title") or item.get("id") or "")

    pending = status.get("pending_deletion") or {}
    count = pending.get("meetings", 0)
    purge = fmt_date(pending.get("purge_at")) or (datetime.now(UTC) + timedelta(days=7)).strftime("%Y-%m-%d")
    if st.button("Disconnect", icon=":material/link_off:"):
        _disconnect_dialog(count, purge)


def _body() -> bool:
    res = fireflies_api.fireflies_status()
    if not res.get("success"):
        _error(res)
        return False
    status = res.get("data") or {}
    projects = known_projects(load_meetings())
    if status.get("linked"):
        _render_linked(status, projects)
    else:
        _render_not_linked(projects)
    pending = status.get("pending_deletion")
    if pending:
        st.warning(f"{pending.get('meetings', 0)} synced meetings will be deleted on {fmt_date(pending.get('purge_at'))}.")
    running = bool(status.get("sync_running"))
    if st.session_state.get(RUNNING_KEY) and not running:
        st.session_state[RUNNING_KEY] = False
        st.rerun()  # sync finished: refresh the rest of the page too
    st.session_state[RUNNING_KEY] = running
    return running


_idle_panel = st.fragment(_body)
_running_panel = st.fragment(run_every=5)(_body)


def render_fireflies_panel() -> bool:
    """Renders the panel; returns whether a sync is running. Auto-refreshes every 5 s only while syncing."""
    with st.container(border=True):
        st.subheader("Fireflies account")
        if st.session_state.get(RUNNING_KEY):
            return bool(_running_panel())
        return bool(_idle_panel())
