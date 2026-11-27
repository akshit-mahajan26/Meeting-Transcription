from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Determine parent directories for environment file resolution
ENGINE_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(ENGINE_DIR / ".env")
load_dotenv(".env")


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
    # Embedding Configuration
    embedding_model: str = Field(
        default="gemini-embedding-001",
        validation_alias=AliasChoices("EMBEDDING_MODEL", "embedding_model", "EMBEDDING_MODEL_NAME"),
        description="Model name for high-dimensional vector embeddings",
    )
    embedding_dimensions: int = Field(
        default=1536,
        validation_alias=AliasChoices("EMBEDDING_DIMENSIONS", "embedding_dimensions", "EMBEDDING_DIMENSION"),
        description="Vector dimension matching embedding model and pgvector column definition",
    )

    # Most similar turns pulled from the database per vector search; 0 removes the cap
    retrieval_candidate_limit: int = Field(
        default=1000,
        ge=0,
        validation_alias=AliasChoices("RETRIEVAL_CANDIDATE_LIMIT", "retrieval_candidate_limit"),
    )

    # Segmentation Parameters
    topic_similarity_threshold: float = Field(
        default=0.55,
        validation_alias=AliasChoices("BOUNDARY_SIMILARITY_THRESHOLD", "TOPIC_SIMILARITY_THRESHOLD", "topic_similarity_threshold"),
        description="Topic boundary cutoff: a similarity below this between neighbouring turns starts a new segment",
    )
    min_turns_per_segment: int = Field(default=2)
    max_turn_tokens: int = Field(
        default=0,
        ge=0,
        validation_alias=AliasChoices("MAX_TURN_TOKENS", "max_turn_tokens"),
        description="Split a speaker's turn once it passes this many words (tokens approximated by words); 0 = never split",
    )
    chunk_overlap_sentences: int = Field(
        default=0,
        ge=0,
        validation_alias=AliasChoices("CHUNK_OVERLAP_SENTENCES", "chunk_overlap_sentences"),
        description="Sentences repeated at the start of the next turn when a long turn is split by max_turn_tokens",
    )
    max_turns_per_segment: int = Field(default=10)

    # HNSW vector search (plan 23.5): applied with SET LOCAL around each vector query (pgvector >= 0.8.0)
    hnsw_ef_search: int = Field(default=100, ge=1, le=1000, validation_alias=AliasChoices("HNSW_EF_SEARCH", "hnsw_ef_search"))
    hnsw_max_scan_tuples: int = Field(
        default=20000, ge=1, validation_alias=AliasChoices("HNSW_MAX_SCAN_TUPLES", "hnsw_max_scan_tuples")
    )

    # Request limits (plan 23.4); must match FS-backend
    query_max_chars: int = Field(default=2000, ge=1, validation_alias=AliasChoices("QUERY_MAX_CHARS", "query_max_chars"))
    history_max_messages: int = Field(
        default=10, ge=0, validation_alias=AliasChoices("HISTORY_MAX_MESSAGES", "history_max_messages")
    )
    history_message_max_chars: int = Field(
        default=4000, ge=1, validation_alias=AliasChoices("HISTORY_MESSAGE_MAX_CHARS", "history_message_max_chars")
    )

    # Gemini concurrency and retries (plan 23.4): chat and ingestion use separate limits
    llm_chat_max_concurrency: int = Field(
        default=16, ge=1, validation_alias=AliasChoices("LLM_CHAT_MAX_CONCURRENCY", "llm_chat_max_concurrency")
    )
    llm_ingest_max_concurrency: int = Field(
        default=8, ge=1, validation_alias=AliasChoices("LLM_INGEST_MAX_CONCURRENCY", "llm_ingest_max_concurrency")
    )
    embed_chat_max_concurrency: int = Field(
        default=8, ge=1, validation_alias=AliasChoices("EMBED_CHAT_MAX_CONCURRENCY", "embed_chat_max_concurrency")
    )
    embed_ingest_max_concurrency: int = Field(
        default=4, ge=1, validation_alias=AliasChoices("EMBED_INGEST_MAX_CONCURRENCY", "embed_ingest_max_concurrency")
    )
    llm_max_retries: int = Field(default=3, ge=0, validation_alias=AliasChoices("LLM_MAX_RETRIES", "llm_max_retries"))
    llm_timeout_seconds: float = Field(
        default=60.0, gt=0, validation_alias=AliasChoices("LLM_TIMEOUT_SECONDS", "llm_timeout_seconds")
    )

    # Database connection pool (plan 23.6)
    db_pool_size: int = Field(default=10, ge=1, validation_alias=AliasChoices("DB_POOL_SIZE", "db_pool_size"))
    db_max_overflow: int = Field(default=10, ge=0, validation_alias=AliasChoices("DB_MAX_OVERFLOW", "db_max_overflow"))
    db_pool_timeout: int = Field(default=30, ge=1, validation_alias=AliasChoices("DB_POOL_TIMEOUT", "db_pool_timeout"))
    db_pool_recycle: int = Field(default=1800, ge=-1, validation_alias=AliasChoices("DB_POOL_RECYCLE", "db_pool_recycle"))

    @model_validator(mode="after")
    def check_production_secrets(self) -> "Settings":
        """Production needs a strong shared secret with FS-backend (plan 23.8)."""
        if self.ai_engine_env.lower() == "production" and len(self.engine_api_key) < 32:
            raise ValueError("ENGINE_API_KEY must be at least 32 characters when AI_ENGINE_ENV=production")
        return self

    @property
    def llm_model(self) -> str:
        return self.LLM_MODEL_NAME

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
