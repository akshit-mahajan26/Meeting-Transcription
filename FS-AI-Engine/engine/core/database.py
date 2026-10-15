import logging
import os

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import settings

logger = logging.getLogger(__name__)

# Create async engine dynamically from environment or settings.database_url
db_url = os.getenv("DATABASE_URL") or settings.database_url
if db_url:
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
else:
    logger.warning("DATABASE_URL is not configured. Async database engine disabled.")
    async_engine = None
    AsyncSessionLocal = None

