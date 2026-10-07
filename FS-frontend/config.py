import os
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class FrontendConfig(BaseSettings):
    """
    Centralized Frontend Configuration for the Streamlit Chat Application.
    Dynamically loads backend endpoints and UI defaults from environment variables or .env.
    """
    FASTAPI_BACKEND_URL: str = Field(
        default="http://localhost:8000",
        description="FastAPI Backend base URL"
    )
    BACKEND_API_URL: str = Field(
        default="http://localhost:8000/api/v1",
        description="FastAPI Backend API v1 URL"
    )
    QUERY_ENDPOINT: str = Field(
        default="/api/v1/query",
        description="Dual-Mode query API path"
    )
    HEALTH_ENDPOINT: str = Field(
        default="/health",
        description="Backend health check path"
    )
    APP_TITLE: str = Field(
        default="Hierarchical Meeting Intelligence",
        description="Streamlit application title"
    )
    APP_ICON: str = Field(
        default="🎙️",
        description="Favicon and header emoji"
    )
    DEFAULT_PROJECT_ID: str = Field(
        default="proj-fireflies-ai",
        description="Default enterprise project ID filter"
    )
    REQUEST_TIMEOUT: float = Field(
        default=30.0,
        description="HTTP request timeout in seconds"
    )

    model_config = SettingsConfigDict(
        env_file=[".env", "FS-frontend/.env", "frontend/.env"],
        env_file_encoding="utf-8",
        extra="ignore"
    )


frontend_config = FrontendConfig()
