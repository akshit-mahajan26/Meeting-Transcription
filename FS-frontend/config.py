import os
from typing import Any

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _populate_env_from_streamlit_secrets() -> None:
    """
    Streamlit Cloud stores secrets in st.secrets, but does not inject them
    into os.environ. This bridges secrets into os.environ so Pydantic BaseSettings
    and standard libraries can access them seamlessly.
    """
    try:
        import streamlit as st
        # accessing st.secrets may raise StreamlitSecretNotFoundError if no secrets file exists
        if hasattr(st, "secrets"):
            for k, v in st.secrets.items():
                if isinstance(v, (str, int, float, bool)):
                    os.environ[k] = str(v)
                    os.environ[k.upper()] = str(v)
                elif isinstance(v, dict):
                    for sub_k, sub_v in v.items():
                        if isinstance(sub_v, (str, int, float, bool)):
                            os.environ[f"{k}_{sub_k}".upper()] = str(sub_v)
                            os.environ[sub_k.upper()] = str(sub_v)
    except Exception:
        # Expected locally when no .streamlit/secrets.toml file exists
        pass


_populate_env_from_streamlit_secrets()


class FrontendConfig(BaseSettings):
    """
    Centralized Frontend Configuration for the Streamlit Chat Application.
    Dynamically loads backend endpoints and UI defaults from environment variables,
    Streamlit secrets, or .env files.
    """
    FASTAPI_BACKEND_URL: str = Field(
        validation_alias=AliasChoices("FASTAPI_BACKEND_URL", "BACKEND_URL", "fastapi_backend_url", "backend_url"),
        description="FastAPI Backend base URL (must be provided via environment, Streamlit secrets, or .env)"
    )
    QUERY_ENDPOINT: str = Field(
        default="/api/v1/query",
        description="Dual-Mode query API path"
    )
    REQUEST_TIMEOUT: float = Field(
        default=120.0,
        description="HTTP request timeout in seconds"
    )
    STREAM_RESPONSES: bool = Field(
        default=True,
        description="Show answers as they are written (server-sent events). Off: wait for the complete answer."
    )

    model_config = SettingsConfigDict(
        env_file=[".env", "FS-frontend/.env", "frontend/.env"],
        env_file_encoding="utf-8",
        extra="ignore"
    )

    def model_post_init(self, __context: Any) -> None:
        self.FASTAPI_BACKEND_URL = self.FASTAPI_BACKEND_URL.rstrip("/")


frontend_config = FrontendConfig()

