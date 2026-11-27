from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
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
    cors_origins: list[str] | str = ["*"]

    # Set false to stop new accounts from registering (existing accounts keep working)
    allow_signup: bool = Field(
        default=True,
        validation_alias=AliasChoices("ALLOW_SIGNUP", "allow_signup"),
    )

    # Database configuration
    database_url: str = Field(
        validation_alias=AliasChoices("DATABASE_URL", "database_url"),
        description="PostgreSQL database connection string (must be provided via environment or .env)",
    )

    # Fireflies API Credentials
    fireflies_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("FIREFLIES_API_KEY", "fireflies_api_key"),
    )
    fireflies_api_url: str = Field(
        default="https://api.fireflies.ai/graphql",
        validation_alias=AliasChoices("FIREFLIES_API_URL", "fireflies_api_url"),
    )

    # FS-AI-Engine endpoint
    ai_engine_url: str = Field(
        validation_alias=AliasChoices("AI_ENGINE_URL", "ai_engine_url", "FS_AI_ENGINE_URL"),
    )
    ai_engine_timeout_seconds: float = Field(
        default=300.0,
        validation_alias=AliasChoices("AI_ENGINE_TIMEOUT_SECONDS", "ai_engine_timeout_seconds"),
        description="Timeout in seconds when communicating with FS-AI-Engine for deep processing",
    )

    # Authentication (JWT bearer tokens issued by this backend)
    jwt_secret: str = Field(
        validation_alias=AliasChoices("JWT_SECRET", "jwt_secret"),
        min_length=16,
        description="Secret used to sign access tokens (must be provided via environment or .env)",
    )
    jwt_algorithm: str = Field(default="HS256")
    access_token_expire_minutes: int = Field(
        default=720,
        validation_alias=AliasChoices("ACCESS_TOKEN_EXPIRE_MINUTES", "access_token_expire_minutes"),
    )

    # Shared secret sent to FS-AI-Engine so only this backend can call it
    engine_api_key: str = Field(
        validation_alias=AliasChoices("ENGINE_API_KEY", "engine_api_key"),
        min_length=16,
        description="Internal API key shared with FS-AI-Engine (must be provided via environment or .env)",
    )

    # Raw Fireflies transcript JSON files (plan 23.7): off by default; when on, stored per account
    save_raw_transcripts: bool = Field(
        default=False,
        validation_alias=AliasChoices("SAVE_RAW_TRANSCRIPTS", "save_raw_transcripts"),
    )
    raw_transcripts_dir: str = Field(
        default="transcripts",
        validation_alias=AliasChoices("RAW_TRANSCRIPTS_DIR", "raw_transcripts_dir"),
        description="Folder for raw transcript files; a relative path is resolved from the project root",
    )

    # Request limits (plan 23.4)
    query_max_chars: int = Field(default=2000, ge=1, validation_alias=AliasChoices("QUERY_MAX_CHARS", "query_max_chars"))
    history_max_messages: int = Field(
        default=10, ge=0, validation_alias=AliasChoices("HISTORY_MAX_MESSAGES", "history_max_messages")
    )
    history_message_max_chars: int = Field(
        default=4000, ge=1, validation_alias=AliasChoices("HISTORY_MESSAGE_MAX_CHARS", "history_message_max_chars")
    )
    max_request_body_bytes: int = Field(
        default=1_048_576, ge=1024, validation_alias=AliasChoices("MAX_REQUEST_BODY_BYTES", "max_request_body_bytes")
    )

    # Database connection pool (plan 23.6)
    db_pool_size: int = Field(default=5, ge=1, validation_alias=AliasChoices("DB_POOL_SIZE", "db_pool_size"))
    db_max_overflow: int = Field(default=5, ge=0, validation_alias=AliasChoices("DB_MAX_OVERFLOW", "db_max_overflow"))
    db_pool_timeout: int = Field(default=30, ge=1, validation_alias=AliasChoices("DB_POOL_TIMEOUT", "db_pool_timeout"))
    db_pool_recycle: int = Field(default=1800, ge=-1, validation_alias=AliasChoices("DB_POOL_RECYCLE", "db_pool_recycle"))

    @property
    def FIREFLIES_API_KEY(self) -> str | None:
        return self.fireflies_api_key

    @property
    def FIREFLIES_API_URL(self) -> str:
        return self.fireflies_api_url

    @property
    def AI_ENGINE_URL(self) -> str:
        return self.ai_engine_url

    @field_validator("database_url", mode="before")
    @classmethod
    def assemble_database_url(cls, v: str) -> str:
        # Name the psycopg 3 driver explicitly: a bare postgresql:// URL selects whichever driver the installed SQLAlchemy defaults to.
        if isinstance(v, str):
            for bare in ("postgres://", "postgresql://"):
                if v.startswith(bare):
                    return "postgresql+psycopg://" + v[len(bare):]
        return v

    @field_validator("cors_origins", mode="before")
    @classmethod
    def assemble_cors_origins(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str) and not v.startswith("["):
            return [i.strip() for i in v.split(",") if i.strip()]
        return v

    model_config = SettingsConfigDict(
        env_file=[str(BACKEND_DIR / ".env"), ".env"],
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Return cached singleton settings."""
    return Settings()
