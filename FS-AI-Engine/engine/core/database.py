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
elif db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql+asyncpg://", 1)

if "sslmode=" in db_url:
    db_url = db_url.replace("sslmode=", "ssl=")

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
            "projects": {},
            "meetings": {},
            "topics": {},
            "turns": {},
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
