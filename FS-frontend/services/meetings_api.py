"""Meeting actions on the backend: set a meeting's project, retry a failed ingestion."""

from __future__ import annotations

from typing import Any

from .api_request import backend_call


def set_meeting_project(meeting_id: str, project_name: str | None) -> dict[str, Any]:
    """PATCH /meetings/{id}; `None` removes the meeting from its project."""
    return backend_call("PATCH", f"/meetings/{meeting_id}", json={"project_id": project_name})


def retry_meeting(meeting_id: str) -> dict[str, Any]:
    """POST /meetings/{id}/retry: 202 queued, or 409/429 with the backend's message."""
    return backend_call("POST", f"/meetings/{meeting_id}/retry")
