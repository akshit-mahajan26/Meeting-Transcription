"""Thin wrappers over the backend's /fireflies endpoints. Each returns backend_call's result dict."""

from __future__ import annotations

from typing import Any

from .api_request import backend_call


def fireflies_status() -> dict[str, Any]:
    return backend_call("GET", "/fireflies/status")


def fireflies_connect(password: str, api_key: str, sync_project: str | None = None) -> dict[str, Any]:
    body = {"password": password, "api_key": api_key, "sync_project": sync_project}
    return backend_call("POST", "/fireflies/connect", json=body, timeout=60.0)


def fireflies_update_settings(auto_sync: bool | None = None, sync_project: str | None = None) -> dict[str, Any]:
    """Sends only the given fields. sync_project="" clears the project (the backend stores null)."""
    body: dict[str, Any] = {}
    if auto_sync is not None:
        body["auto_sync"] = auto_sync
    if sync_project is not None:
        body["sync_project"] = sync_project
    return backend_call("PATCH", "/fireflies/settings", json=body)


def fireflies_sync() -> dict[str, Any]:
    return backend_call("POST", "/fireflies/sync")


def fireflies_disconnect(password: str) -> dict[str, Any]:
    return backend_call("POST", "/fireflies/disconnect", json={"password": password})
