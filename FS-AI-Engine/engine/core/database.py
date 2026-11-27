import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import settings

logger = logging.getLogger(__name__)

# Create async engine dynamically from environment or settings.database_url
db_url = settings.database_url
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
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout,
            pool_recycle=settings.db_pool_recycle,
            future=True,
        )
        logger.info(
            "DB pool: size=%s max_overflow=%s timeout=%ss recycle=%ss",
            settings.db_pool_size,
            settings.db_max_overflow,
            settings.db_pool_timeout,
            settings.db_pool_recycle,
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

