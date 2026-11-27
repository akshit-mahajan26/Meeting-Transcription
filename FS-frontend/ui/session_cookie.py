"""The `mi_session` login cookie, written and deleted by a small CCv2 component (runs in the app page, same origin)."""

from __future__ import annotations

import itertools
import logging
from datetime import UTC, datetime

import streamlit as st
from config import frontend_config

COOKIE_NAME = "mi_session"

_JS = """
export default function (component) {
  const { data } = component;
  const name = "mi_session";
  const secure = location.protocol === "https:" ? "; Secure" : "";
  if (data && data.op === "set") {
    const age = data.max_age ? "; Max-Age=" + data.max_age : "";
    document.cookie = name + "=" + encodeURIComponent(data.token) + "; Path=/; SameSite=Strict" + age + secure;
  }
  if (data && data.op === "del") {
    document.cookie = name + "=; Path=/; Max-Age=0; SameSite=Strict" + secure;
  }
}
"""

_component = st.components.v2.component("mi_session_cookie", html="<span></span>", js=_JS)
_counter = itertools.count()
logger = logging.getLogger(__name__)


def _max_age(expires_at: str | None) -> int | None:
    if not frontend_config.SESSION_COOKIE_PERSISTENT or not expires_at:
        return None
    try:
        end = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    return max(0, int((end - datetime.now(UTC)).total_seconds()))


def _mount(data: dict) -> None:
    """Best effort: a failure to mount the cookie component must never break the app (login just won't persist)."""
    try:
        _component(data=data, key=f"cookie_{data['op']}_{next(_counter)}")
    except Exception:
        logger.warning(f"Session cookie '{data['op']}' could not be written; the login will not survive a refresh", exc_info=True)


def write(token: str, expires_at: str | None = None) -> None:
    _mount({"op": "set", "token": token, "max_age": _max_age(expires_at)})


def delete() -> None:
    _mount({"op": "del"})
