"""Session endpoints used to restore and refresh a login: GET /auth/session and POST /auth/refresh."""

from __future__ import annotations

from typing import Any

import httpx

from .api_client import _error_detail, api_client


def _call(method: str, path: str, token: str | None) -> dict[str, Any]:
    """Returns {"success": True, "status_code", "data"} or {"success": False, "status_code", "error"}.
    Never touches the session state: the caller decides what a 401 means."""
    if not api_client.base_url:
        return {"success": False, "status_code": None, "error": "FASTAPI_BACKEND_URL is not configured."}
    if not token:
        return {"success": False, "status_code": 401, "error": "No session token."}
    headers = {**api_client._client_ip_headers(), "Authorization": f"Bearer {token}"}
    try:
        with httpx.Client(timeout=min(api_client.timeout, 20.0)) as client:
            res = client.request(method, f"{api_client.base_url}/api/v1/auth{path}", headers=headers)
    except Exception:
        return {"success": False, "status_code": None, "error": "Cannot reach the backend. Try again in a moment."}
    if res.is_success:
        return {"success": True, "status_code": res.status_code, "data": res.json() if res.content else None}
    return {"success": False, "status_code": res.status_code, "error": _error_detail(res)}


def get_session(token: str) -> dict[str, Any]:
    return _call("GET", "/session", token)


def refresh_token() -> dict[str, Any]:
    """Refresh the current session's token (read from the session state)."""
    return _call("POST", "/refresh", api_client._auth_headers().get("Authorization", "").removeprefix("Bearer "))
