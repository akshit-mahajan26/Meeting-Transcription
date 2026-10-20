"""
FS-AI-Engine/graphs/chat_records.py
===================================
Verified database records the synthesis prompt is grounded in:
- format_meeting_date / build_meeting_details: meeting dates and times (UTC).
- build_catalog_records: projects, meetings and participants, with deterministic counts.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

_MAX_CATALOG_MEETING_ROWS = 200
_MAX_DETAIL_MEETINGS = 20


def format_meeting_date(raw: Any) -> str | None:
    """Meeting start as 'YYYY-MM-DD HH:MM UTC'. Fireflies stores epoch milliseconds; ISO strings pass through."""
    if not raw:
        return None
    text = str(raw).strip()
    try:
        number = float(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text)
            parsed = parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
            return parsed.strftime("%Y-%m-%d %H:%M UTC")
        except ValueError:
            return text
    if number <= 0:
        return None
    seconds = number / 1000 if number > 1e11 else number
    return datetime.fromtimestamp(seconds, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")


def build_meeting_details(available_meetings: list[dict[str, Any]], meeting_ids: list[str]) -> str:
    """Verified per-meeting facts (title, ID, project, date and time), so date questions can be answered."""
    by_id = {m["id"]: m for m in available_meetings}
    # Sort distinct meeting IDs by date descending so the newest meetings always appear first
    unique_ids = list(dict.fromkeys(meeting_ids))
    unique_ids.sort(key=lambda mid: str(by_id.get(mid, {}).get("date") or ""), reverse=True)

    lines = []
    for mid in unique_ids[:_MAX_DETAIL_MEETINGS]:
        m = by_id.get(mid)
        if not m:
            continue
        lines.append(
            f"- **{m['title']}** (Project: {m.get('project_id') or 'Unassigned'}, "
            f"Held on: {m.get('date') or 'not recorded'})"
        )
    if not lines:
        return ""
    return "Database Records - Meeting Details (dates and times are UTC):\n" + "\n".join(lines) + "\n\n---\n"


def build_catalog_records(
    *,
    query_lower: str,
    force_all: bool,
    available_projects: list[dict[str, Any]],
    available_meetings: list[dict[str, Any]],
    available_speakers: list[str],
    scoped_to_meeting: bool,
    project_id: str | None,
    has_speakers: bool,
) -> tuple[str, list[str]]:
    """
    Builds the verified database catalog blocks for the synthesis prompt.
    `force_all` is set for catalog-intent questions: the records are always supplied, no matter how the question
    is worded. Otherwise each block is added only when the question mentions its entity. Counts are computed
    here so the model never has to count.
    """
    context = ""
    instructions: list[str] = []
    mentions = lambda *words: any(w in query_lower for w in words)  # noqa: E731

    # Projects (with deterministic meeting counts per project)
    if available_projects and (force_all or mentions("project")):
        per_project: dict[str, int] = {}
        unassigned = 0
        for m in available_meetings:
            if m.get("project_id"):
                per_project[m["project_id"]] = per_project.get(m["project_id"], 0) + 1
            else:
                unassigned += 1
        active = [p for p in available_projects if (p.get("status") or "").upper() == "ACTIVE"]
        lines = "\n".join(
            f"- **{p['name']}** (ID: {p['id']}, Status: {p.get('status', 'ACTIVE')}, "
            f"Meetings: {per_project.get(p['id'], 0)})"
            for p in available_projects
        )
        counts = ", ".join(f"{pid}: {n}" for pid, n in sorted(per_project.items()))
        if unassigned:
            counts += f"{', ' if counts else ''}Unassigned: {unassigned}"
        context += (
            f"Database Catalog - Projects ({len(available_projects)} total, {len(active)} active):\n{lines}\n"
            f"Verified meeting counts per project (computed from the database): {counts or 'none'}; "
            f"total meetings: {len(available_meetings)}\n\n---\n"
        )
        instructions.append(
            "- Answer project and meeting-count questions using the verified Database Catalog above. "
            "Use the computed meeting counts exactly as given; do not recount or estimate."
        )

    # Meetings
    general_meeting_query = mentions("meeting") and mentions(
        "all", "list", "how many", "which", "available", "recorded", "what meetings"
    )
    if available_meetings and (force_all or (general_meeting_query and not scoped_to_meeting and not has_speakers)):
        rows = available_meetings[:_MAX_CATALOG_MEETING_ROWS]
        mtg_lines = "\n".join(
            f"- **{m['title']}** (Project: {m.get('project_id') or 'Unassigned'}, "
            f"Held on: {m.get('date') or 'not recorded'})"
            for m in rows
        )
        more = (
            f"\n(+{len(available_meetings) - len(rows)} more meetings not listed; the total above is exact)"
            if len(available_meetings) > len(rows)
            else ""
        )
        context += (
            f"Database Catalog - Complete List of Meetings ({len(available_meetings)} total):\n"
            f"{mtg_lines}{more}\n\n---\n"
        )
        instructions.append(
            f"- The catalog lists {len(available_meetings)} meetings in total. "
            "When the user asks to list or count meetings, "
            "state the exact number and name the meetings by title."
        )

    # Participants
    participant_query = mentions("speaker", "participant", "attendee", "participate", "attend") and mentions(
        "all", "list", "how many", "who", "which", "available"
    )
    if available_speakers and (force_all or participant_query):
        proj_str = f" for Project '{project_id}'" if project_id else ""
        spk_lines = "\n".join(f"- {s}" for s in available_speakers)
        context += (
            f"Database Catalog - Participants / Speakers ({len(available_speakers)} total{proj_str}):\n"
            f"{spk_lines}\n\n---\n"
        )
        instructions.append(
            f"- When asked about participants/speakers{proj_str}, "
            "enumerate them from the verified Database Catalog above."
        )

    return context, instructions


def _unscope_proj(pid: str | None) -> str | None:
    if pid and ":" in pid and len(pid.split(":", 1)[0]) == 36:
        return pid.split(":", 1)[1]
    return pid


# Date resolution: which meetings does the question's date range refer to? (the range comes from rewrite_query)

def _meeting_day(meeting: dict[str, Any]) -> date | None:
    """Calendar day of a meeting from its stored 'YYYY-MM-DD HH:MM UTC' date, or None if unreadable."""
    try:
        return date.fromisoformat(str(meeting.get("date") or "")[:10])
    except ValueError:
        return None


def _resolve_day(value: str | None, held: list[date]) -> date | None:
    """'YYYY-MM-DD' as given; 'MM-DD' (no year stated) takes the newest year with a meeting on that day."""
    value = (value or "").strip()
    try:
        return date.fromisoformat(value)
    except ValueError:
        pass
    try:
        month, day = (int(part) for part in value.split("-"))
        return date(max((d.year for d in held if (d.month, d.day) == (month, day)), default=datetime.now(UTC).year), month, day)
    except ValueError:
        return None


def meetings_in_date_range(
    meetings: list[dict[str, Any]], date_from: str | None, date_to: str | None
) -> tuple[list[dict[str, Any]], str | None]:
    """
    Meetings held from `date_from` to `date_to` (inclusive, either end may be open), oldest first, plus a label.
    The label is None when no usable date was given; with a label and an empty list, no meeting falls in the range.
    """
    held = [(m, day) for m in meetings if (day := _meeting_day(m))]
    start = _resolve_day(date_from, [day for _, day in held])
    end = _resolve_day(date_to, [day for _, day in held])
    if not start and not end:
        return [], None
    if start and end and start > end:
        start, end = end, start
    matched = [m for m, day in held if (not start or day >= start) and (not end or day <= end)]
    if start and end:
        label = start.isoformat() if start == end else f"{start.isoformat()} to {end.isoformat()}"
    else:
        label = f"{start.isoformat()} onwards" if start else f"{end.isoformat()} and earlier"
    return sorted(matched, key=lambda m: str(m.get("date") or "")), label
