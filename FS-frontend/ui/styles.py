"""Global styling injection for Meeting Intelligence.

Production CSS lives in `ui/css/*.css` (split to stay under the 400-line file limit),
concatenated in filename order.
"""

from pathlib import Path

import streamlit as st

_CSS_DIR = Path(__file__).resolve().parent / "css"


def _load_css() -> str:
    """Read and concatenate stylesheet parts from disk, in filename order."""
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(_CSS_DIR.glob("*.css")))


# Module-level alias for backward compatibility
CSS = f"<style>\n{_load_css()}\n</style>"


def inject_styles() -> None:
    """Inject global CSS into the current Streamlit app page."""
    st.markdown(f"<style>{_load_css()}</style>", unsafe_allow_html=True)
