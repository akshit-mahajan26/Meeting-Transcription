"""Page registry so pages can link to each other via `go("assistant")`."""

from __future__ import annotations

import streamlit as st


def register(pages: dict) -> None:
    st.session_state["_pages"] = pages


def go(name: str) -> None:
    st.switch_page(st.session_state["_pages"][name])
