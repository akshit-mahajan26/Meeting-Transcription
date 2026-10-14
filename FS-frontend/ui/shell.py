"""App shell shared by every page: the signed-in account and log-out button."""

from __future__ import annotations

from ui.auth import render_account_menu


def render_shell() -> None:
    """App shell hook: shows who is signed in."""
    render_account_menu()

