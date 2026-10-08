from functools import lru_cache
from pathlib import Path
from typing import List, Union, Optional
from pydantic import field_validator, Field, AliasChoices
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    """Backend system configuration loaded from environment or .env file."""
    app_name: str = "Meeting Transcript Intelligence Platform - Backend"
    app_version: str = "0.2.0"
    api_v1_str: str = "/api/v1"
    backend_host: str = "0.0.0.0"
    backend_port: int = 8000
    backend_env: str = "development"

    # CORS configuration
    cors_origins: Union[List[str], str] = ["*"]

    # Database configuration
    database_url: str = Field(
        default="postgresql://python:python@localhost:5434/Fireflies",
        validation_alias=AliasChoices("DATABASE_URL", "database_url"),
    )

    # Fireflies Webhook HMAC Secret
    fireflies_webhook_secret: str = Field(
        default="super_secret_webhook_key_12345",
        validation_alias=AliasChoices("FIREFLIES_WEBHOOK_SECRET", "fireflies_webhook_secret"),
    )

    # Fireflies API Credentials
    fireflies_api_key: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("FIREFLIES_API_KEY", "fireflies_api_key"),
    )
    fireflies_api_url: str = Field(
        default="https://api.fireflies.ai/graphql",
        validation_alias=AliasChoices("FIREFLIES_API_URL", "fireflies_api_url"),
    )

    # FS-AI-Engine endpoint
    ai_engine_url: str = Field(
        default="http://localhost:8001",
        validation_alias=AliasChoices("AI_ENGINE_URL", "ai_engine_url"),
    )
    ai_engine_timeout_seconds: float = Field(
        default=300.0,
        validation_alias=AliasChoices("AI_ENGINE_TIMEOUT_SECONDS", "ai_engine_timeout_seconds"),
        description="Timeout in seconds when communicating with FS-AI-Engine for deep processing",
    )

    # Gemini parameters
    gemini_api_key: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("GEMINI_API_KEY", "gemini_api_key"),
    )
    gemini_base_url: str = Field(
        default="https://generativelanguage.googleapis.com/v1beta/openai/",
        validation_alias=AliasChoices("GEMINI_BASE_URL", "gemini_base_url"),
    )

    # OpenRouter parameters
    openrouter_api_key: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("OPENROUTER_API_KEY", "openrouter_api_key"),
    )
    openrouter_base_url: str = Field(
        default="https://openrouter.ai/api/v1",
        validation_alias=AliasChoices("OPENROUTER_BASE_URL", "openrouter_base_url"),
    )
    llm_model_name: str = Field(
        default="gemini-2.5-flash",
        validation_alias=AliasChoices("LLM_MODEL_NAME", "llm_model_name"),
    )

    @property
    def FIREFLIES_API_KEY(self) -> Optional[str]:
        return self.fireflies_api_key

    @property
    def FIREFLIES_API_URL(self) -> str:
        return self.fireflies_api_url

    @property
    def FIREFLIES_WEBHOOK_SECRET(self) -> str:
        return self.fireflies_webhook_secret

    @property
    def AI_ENGINE_URL(self) -> str:
        return self.ai_engine_url

    @field_validator("database_url", mode="before")
    @classmethod
    def assemble_database_url(cls, v: str) -> str:
        if isinstance(v, str) and v.startswith("postgres://"):
            return v.replace("postgres://", "postgresql://", 1)
        return v

    @field_validator("cors_origins", mode="before")
    @classmethod
    def assemble_cors_origins(cls, v: Union[str, List[str]]) -> List[str]:
        if isinstance(v, str) and not v.startswith("["):
            return [i.strip() for i in v.split(",") if i.strip()]
        return v

    model_config = SettingsConfigDict(
        env_file=[str(BACKEND_DIR / ".env"), ".env"],
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache()
def get_settings() -> Settings:
    """Return cached singleton settings."""
    return Settings()
