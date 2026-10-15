"""Pure formatting helpers (no Streamlit imports, safe to unit test)."""

from __future__ import annotations

import html
import re
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

_ID_SUFFIX = re.compile(r"\(([^)]+)\)$")


def esc(value: Any) -> str:
    """HTML-escape any value so transcript text can't inject markup."""
    return html.escape(str(value if value is not None else ""), quote=True)


def format_timestamp(seconds: float | None) -> str:
    """Seconds -> MM:SS (or H:MM:SS for long recordings)."""
    total = int(float(seconds or 0))
    hours, rem = divmod(total, 3600)
    mins, secs = divmod(rem, 60)
    return f"{hours}:{mins:02d}:{secs:02d}" if hours else f"{mins:02d}:{secs:02d}"


def format_meeting_date(value: str | None) -> str:
    """Human-readable meeting start (UTC), or an em dash when the date is unknown."""
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(value)
        parsed = parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        return parsed.strftime("%d %b %Y, %H:%M UTC")
    except ValueError:
        return str(value)


def meeting_label(meeting: dict[str, Any]) -> str:
    title = (meeting.get("title") or "").strip() or meeting.get("id", "")
    return f"{title} ({meeting.get('id')})"


def parse_meeting_id(label: str) -> str | None:
    """Extract the trailing '(id)' from a label built by `meeting_label`."""
    match = _ID_SUFFIX.search(label)
    return match.group(1).strip() if match else None


def merge_projects(existing: Iterable[str], *more: Iterable[str]) -> list[str]:
    merged = {p.strip() for group in (existing, *more) for p in group if p and p.strip()}
    return sorted(merged)


def speaker_talk_time(turns: list[dict[str, Any]]) -> dict[str, float]:
    """Total seconds spoken per speaker, descending."""
    totals: dict[str, float] = defaultdict(float)
    for t in turns:
        spk = t.get("speaker_name") or "Unknown"
        duration = float(t.get("end_time") or 0.0) - float(t.get("start_time") or 0.0)
        totals[spk] += max(duration, 0.0)
    return dict(sorted(totals.items(), key=lambda kv: kv[1], reverse=True))


def transcript_to_markdown(meeting: dict[str, Any], turns: list[dict[str, Any]]) -> str:
    lines = [f"# {meeting.get('title') or meeting.get('id')}", ""]
    if meeting.get("project_id"):
        lines += [f"**Project:** {meeting['project_id']}", ""]
    if (meeting.get("summary") or "").strip():
        lines += ["## Summary", meeting["summary"].strip(), ""]
    lines.append("## Transcript")
    for t in turns:
        lines.append(
            f"**[{format_timestamp(t.get('start_time'))}] {t.get('speaker_name') or 'Unknown'}:** "
            f"{t.get('text') or ''}"
        )
        lines.append("")
    return "\n".join(lines)


def chat_to_markdown(messages: list[dict[str, Any]]) -> str:
    lines = ["# Meeting Intelligence conversation", ""]
    for msg in messages:
        if msg.get("role") == "user":
            lines += [f"**You:** {msg.get('content', '')}", ""]
        else:
            lines += [f"**Assistant:** {msg.get('content', '')}", ""]
            for i, c in enumerate(msg.get("citations") or [], 1):
                lines.append(
                    f"> [{i}] {c.get('speaker') or 'Unknown'} @ "
                    f"{format_timestamp(c.get('start_time'))}: {c.get('exact_raw_text') or ''}"
                )
            lines.append("")
    return "\n".join(lines)

