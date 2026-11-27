"""One backend call with the shared client's rules: bearer token, forwarded browser IP, 401 = session expired,
every other error returned as the backend's message (429, 413, 422, 409, 403, 503 never log the user out)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .api_client import _error_detail, api_client

logger = logging.getLogger(__name__)


def backend_call(method: str, path: str, json: dict[str, Any] | None = None, timeout: float = 20.0) -> dict[str, Any]:
    """Calls `{FASTAPI_BACKEND_URL}/api/v1{path}`.

    Returns {"success": True, "status_code", "data"} or {"success": False, "status_code", "error"}.
    """
    if not api_client.base_url:
        return {"success": False, "status_code": None, "error": "FASTAPI_BACKEND_URL is not configured."}
    url = f"{api_client.base_url}/api/v1{path}"
    try:
        with httpx.Client(timeout=min(api_client.timeout, timeout)) as client:
            res = client.request(method, url, json=json, headers=api_client._request_headers())
    except Exception as exc:
        logger.warning(f"Backend call {method} {path} failed: {exc}")
        return {"success": False, "status_code": None, "error": "Cannot reach the backend. Try again in a moment."}
    if res.status_code == 401:
        api_client._expire_session()
    if res.is_success:
        data = res.json() if res.content else None
        return {"success": True, "status_code": res.status_code, "data": data}
    return {"success": False, "status_code": res.status_code, "error": _error_detail(res)}
