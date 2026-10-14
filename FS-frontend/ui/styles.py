"""Global styling injection for Meeting Intelligence.

All production CSS lives in `ui/style.css` for proper syntax highlighting,
IDE linting, and clean frontend developer maintenance.
"""

from pathlib import Path

import streamlit as st

_CSS_PATH = Path(__file__).resolve().parent / "style.css"


def _load_css() -> str:
    """Read stylesheet content from disk."""
    return _CSS_PATH.read_text(encoding="utf-8")


# Module-level alias for backward compatibility
CSS = f"<style>\n{_load_css()}\n</style>"


def inject_styles() -> None:
    """Inject global CSS into the current Streamlit app page."""
    st.markdown(f"<style>{_load_css()}</style>", unsafe_allow_html=True)
