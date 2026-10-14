from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Determine parent directories for environment file resolution
ENGINE_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(ENGINE_DIR / ".env", override=True)
load_dotenv(".env", override=True)


class Settings(BaseSettings):
    """
    Centralized Dynamic Configuration Module.
    Zero Hardcoding: All parameters are dynamically resolved from environment variables or .env.
    """
    # Application runtime configuration
    app_name: str = Field(default="Meeting Transcript Intelligence - AI Engine")
    app_version: str = Field(default="0.5.0")
    ai_engine_host: str = Field(default="0.0.0.0")
    ai_engine_port: int = Field(default=8001)
    ai_engine_env: str = Field(default="development")

    # Database Configuration (PostgreSQL with asyncpg for Phase 3 & 4)
    database_url: str = Field(
        validation_alias=AliasChoices("DATABASE_URL", "database_url"),
        description="Async PostgreSQL connection string (must be provided via environment or .env)",
    )

    # Internal API key: only FS-backend (which verifies user logins) may call this engine
    engine_api_key: str = Field(
        validation_alias=AliasChoices("ENGINE_API_KEY", "engine_api_key"),
        min_length=16,
        description="Shared secret expected in the X-Internal-Api-Key header (must be provided via environment or .env)",
    )

    # Gemini API & Universal LLM Configuration
    GEMINI_API_KEY: str | None = Field(
        default=None,
        validation_alias=AliasChoices("GEMINI_API_KEY", "gemini_api_key", "GOOGLE_API_KEY"),
        description="Google Gemini universal API key",
    )
    GEMINI_BASE_URL: str = Field(
        default="https://generativelanguage.googleapis.com/v1beta/openai/",
        validation_alias=AliasChoices("GEMINI_BASE_URL", "gemini_base_url"),
        description="Gemini OpenAI-compatible API Base URL",
    )

    # Default LLM Model (Google Gemini)
    LLM_MODEL_NAME: str = Field(
        default="gemini-2.5-flash",
        validation_alias=AliasChoices("LLM_MODEL_NAME", "LLM_MODEL", "llm_model"),
        description="Primary LLM model string (e.g. gemini-2.5-flash, gemini-2.0-flash)",
    )
    llm_temperature: float = Field(
        default=0.0,
        description="Sampling temperature for deterministic boundary verification",
    )

    # Embedding Configuration
    embedding_model: str = Field(
        default="text-embedding-3-small",
        validation_alias=AliasChoices("EMBEDDING_MODEL", "embedding_model", "EMBEDDING_MODEL_NAME"),
        description="Model name for high-dimensional vector embeddings",
    )
    embedding_dimensions: int = Field(
        default=1536,
        validation_alias=AliasChoices("EMBEDDING_DIMENSIONS", "embedding_dimensions", "EMBEDDING_DIMENSION"),
        description="Vector dimension matching embedding model and pgvector column definition",
    )

    # Boundary Detection & Chunking Parameters
    boundary_similarity_threshold: float = Field(
        default=0.75,
        validation_alias=AliasChoices("BOUNDARY_SIMILARITY_THRESHOLD", "boundary_similarity_threshold"),
        description="Cosine similarity threshold below which a topic shift candidate is triggered",
    )
    max_turn_tokens: int = Field(
        default=400,
        validation_alias=AliasChoices("MAX_TURN_TOKENS", "max_turn_tokens"),
        description="Maximum token length for atomic speaker turns before triggering sub-splitting",
    )
    chunk_overlap_sentences: int = Field(
        default=2,
        validation_alias=AliasChoices("CHUNK_OVERLAP_SENTENCES", "chunk_overlap_sentences"),
        description="Sentence overlap window between consecutive sub-chunks",
    )

    # Backward Compatibility & Dynamic Accessors
    topic_similarity_threshold: float = Field(default=0.55)
    min_turns_per_segment: int = Field(default=2)
    max_turns_per_segment: int = Field(default=10)
    llm_provider: str = Field(default="openrouter")
    llm_api_key: str | None = Field(default=None)

    @property
    def openrouter_api_key(self) -> str | None:
        return self.OPENROUTER_API_KEY

    @property
    def openrouter_base_url(self) -> str:
        return self.OPENROUTER_BASE_URL

    @property
    def llm_model(self) -> str:
        return self.LLM_MODEL_NAME

    @property
    def EMBEDDING_MODEL_NAME(self) -> str:
        return self.embedding_model

    @property
    def EMBEDDING_DIMENSION(self) -> int:
        return self.embedding_dimensions

    @property
    def MACRO_SEARCH_LIMIT(self) -> int:
        return 10

    @property
    def SEARCH_LIMIT(self) -> int:
        return 5

    @property
    def SIMILARITY_THRESHOLD(self) -> float:
        return self.boundary_similarity_threshold

    @property
    def ASYNC_DATABASE_URL(self) -> str:
        return self.database_url

    @property
    def is_gemini_configured(self) -> bool:
        return bool(self.GEMINI_API_KEY and self.GEMINI_API_KEY.strip())

    model_config = SettingsConfigDict(
        env_file=[str(ENGINE_DIR / ".env"), ".env"],
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Return cached singleton instance of Settings."""
    return Settings()


# Exported single instantiated settings object for dependency injection
settings: Settings = get_settings()
