"""Login / sign-up page and the logged-in session (kept per browser session in st.session_state)."""

from __future__ import annotations

from typing import Any

import streamlit as st
from services.api_client import SESSION_EXPIRED_KEY, TOKEN_KEY, api_client

USER_KEY = "auth_user"
MIN_PASSWORD_LENGTH = 6


def is_logged_in() -> bool:
    return bool(st.session_state.get(TOKEN_KEY)) and bool(st.session_state.get(USER_KEY))


def current_user() -> dict[str, Any]:
    return st.session_state.get(USER_KEY) or {}


def current_user_id() -> str:
    """Account id used to key cached data, so one account never sees another's cache."""
    return str(current_user().get("id", ""))


def _start_session(result: dict[str, Any]) -> None:
    st.session_state.clear()  # nothing from a previous visitor survives into this login
    st.session_state[TOKEN_KEY] = result["token"]
    st.session_state[USER_KEY] = result["user"]
    st.rerun()


def logout() -> None:
    st.session_state.clear()
    st.rerun()


def _login_form() -> None:
    with st.form("login_form"):
        email = st.text_input("Email", placeholder="you@company.com", autocomplete="email")
        password = st.text_input("Password", type="password", autocomplete="current-password")
        submitted = st.form_submit_button("Log in", type="primary", width="stretch")
    if submitted:
        if not email.strip() or not password:
            st.warning("Enter your email and password.", icon=":material/lock:")
            return
        with st.spinner("Signing in…"):
            result = api_client.login(email.strip(), password)
        if result["success"]:
            _start_session(result)
        else:
            st.error(result["error"], icon=":material/error:")


def _signup_form() -> None:
    with st.form("signup_form"):
        email = st.text_input("Email", placeholder="you@company.com", autocomplete="email", key="signup_email")
        password = st.text_input(
            "Password", type="password", autocomplete="new-password", key="signup_password",
            help=f"At least {MIN_PASSWORD_LENGTH} characters.",
        )
        confirm = st.text_input("Confirm password", type="password", autocomplete="new-password", key="signup_confirm")
        submitted = st.form_submit_button("Create account", type="primary", width="stretch")
    if submitted:
        if not email.strip() or not password:
            st.warning("Enter an email and a password.", icon=":material/lock:")
        elif len(password) < MIN_PASSWORD_LENGTH:
            st.warning(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.", icon=":material/lock:")
        elif password != confirm:
            st.warning("The two passwords do not match.", icon=":material/lock:")
        else:
            with st.spinner("Creating your account…"):
                result = api_client.signup(email.strip(), password)
            if result["success"]:
                _start_session(result)
            else:
                st.error(result["error"], icon=":material/error:")


def render_auth_page() -> None:
    """Shown instead of the app until the user is logged in."""
    expired = st.session_state.pop(SESSION_EXPIRED_KEY, False)
    _, centre, _ = st.columns([1, 2, 1])
    with centre:
        st.markdown('<div class="page-title">Meeting Intelligence</div>'
                    '<div class="page-sub">Log in to your account to open your projects and meetings.</div>',
                    unsafe_allow_html=True)
        if expired:
            st.info("Your session has expired. Please log in again.", icon=":material/schedule:")
        login_tab, signup_tab = st.tabs(["Log in", "Sign up"])
        with login_tab:
            _login_form()
        with signup_tab:
            _signup_form()


def render_account_menu() -> None:
    """Who is logged in, with a log-out button (sidebar)."""
    with st.sidebar:
        st.caption(f"Signed in as **{current_user().get('email', '')}**")
        if st.button("Log out", icon=":material/logout:", width="stretch", key="logout_button"):
            logout()
