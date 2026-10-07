import logging
from typing import AsyncGenerator, Dict, Any, List, Optional
from contextlib import asynccontextmanager
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from .config import settings

logger = logging.getLogger(__name__)

# Create async engine dynamically from settings.database_url
db_url = settings.database_url
if db_url.startswith("postgresql://"):
    db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)

try:
    async_engine = create_async_engine(
        db_url,
        echo=False,
        pool_pre_ping=True,
        future=True,
    )
    AsyncSessionLocal = async_sessionmaker(
        bind=async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )
except Exception as e:
    logger.warning(f"Could not initialize async_engine ({e}). Falling back to mock session.")
    async_engine = None
    AsyncSessionLocal = None


async def get_async_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency provider for async SQLAlchemy database session."""
    if AsyncSessionLocal is None:
        yield None
        return
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


class DatabaseSessionManager:
    """
    Manages async database sessions with automatic resilience.
    Uses async PostgreSQL engine when available; maintains an in-memory
    mock store during offline testing or when Postgres is unavailable.
    """

    def __init__(self):
        self._in_memory_records: Dict[str, Dict[str, Any]] = {
            "projects": {
                "proj-fireflies-ai": {
                    "id": "proj-fireflies-ai",
                    "name": "Fireflies Meeting Intelligence",
                    "status": "active",
                    "health_score": 0.98,
                    "description": "Enterprise hierarchical RAG platform for meeting transcript intelligence."
                }
            },
            "meetings": {
                "mtg-eng-sync-2026-09-15": {
                    "id": "mtg-eng-sync-2026-09-15",
                    "project_id": "proj-fireflies-ai",
                    "title": "Engineering Architecture & State Persistence",
                    "executive_summary": "Evaluated LangGraph checkpointers. Chose PostgreSQL with AsyncPostgresSaver.",
                    "status": "processed",
                    "meeting_date": "2026-09-15T10:00:00Z"
                },
                "mtg-prod-launch-2026-09-22": {
                    "id": "mtg-prod-launch-2026-09-22",
                    "project_id": "proj-fireflies-ai",
                    "title": "Production Launch Readiness & SLA Guardrails",
                    "executive_summary": "Confirmed 5-second SLA for webhooks via Celery asynchronous offloading.",
                    "status": "processed",
                    "meeting_date": "2026-09-22T14:30:00Z"
                }
            },
            "topics": {
                "top-001": {
                    "id": "top-001",
                    "meeting_id": "mtg-eng-sync-2026-09-15",
                    "topic_name": "Database Architecture & Checkpointer",
                    "summary": "Selection of PostgreSQL over Redis for LangGraph checkpointer persistence."
                },
                "top-002": {
                    "id": "top-002",
                    "meeting_id": "mtg-prod-launch-2026-09-22",
                    "topic_name": "Webhook Ingestion & SLA Guardrails",
                    "summary": "5-second response time requirement and Celery async workers."
                }
            },
            "turns": {
                "turn-001": {
                    "id": "turn-001",
                    "meeting_id": "mtg-eng-sync-2026-09-15",
                    "topic_id": "top-001",
                    "speaker": "Sarah Chen",
                    "start_time": 45.2,
                    "end_time": 68.0,
                    "text": "We decided on PostgreSQL for the LangGraph checkpointer because we need durable persistence across server restarts.",
                    "raw_text": "We decided on PostgreSQL for the LangGraph checkpointer because we need durable persistence across server restarts.",
                    "embedding": None
                },
                "turn-002": {
                    "id": "turn-002",
                    "meeting_id": "mtg-prod-launch-2026-09-22",
                    "topic_id": "top-002",
                    "speaker": "Marcus Vance",
                    "start_time": 12.0,
                    "end_time": 34.5,
                    "text": "The Fireflies webhook requires an immediate HTTP 200 response within 5 seconds, so we offload ingestion to Celery.",
                    "raw_text": "The Fireflies webhook requires an immediate HTTP 200 response within 5 seconds, so we offload ingestion to Celery.",
                    "embedding": None
                }
            }
        }

    @asynccontextmanager
    async def get_session(self) -> AsyncGenerator[Optional[AsyncSession], None]:
        if AsyncSessionLocal is not None:
            try:
                async with AsyncSessionLocal() as session:
                    yield session
                    return
            except Exception as e:
                logger.warning(f"Async session error ({e}); operating via in-memory store.")
        yield None

    def get_in_memory_records(self) -> Dict[str, Dict[str, Any]]:
        return self._in_memory_records


db_manager = DatabaseSessionManager()
