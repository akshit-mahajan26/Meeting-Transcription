import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import settings

logger = logging.getLogger(__name__)

# Read-only access to the knowledge graph database (written by FS-backend); disabled when KG_DATABASE_URL is unset
kg_url = settings.kg_database_url
if kg_url:
    for bare in ("postgresql://", "postgres://", "postgresql+psycopg://"):
        if kg_url.startswith(bare):
            kg_url = "postgresql+asyncpg://" + kg_url[len(bare):]
            break
    if "sslmode=" in kg_url:
        kg_url = kg_url.replace("sslmode=", "ssl=")

    try:
        kg_async_engine = create_async_engine(kg_url, echo=False, pool_pre_ping=True, future=True)
        KGAsyncSessionLocal = async_sessionmaker(
            bind=kg_async_engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autocommit=False,
            autoflush=False,
        )
    except Exception as e:
        logger.warning(f"Could not initialize the knowledge graph engine ({e}). Graph retrieval disabled.")
        kg_async_engine = None
        KGAsyncSessionLocal = None
else:
    logger.info("KG_DATABASE_URL is not configured. Graph retrieval disabled.")
    kg_async_engine = None
    KGAsyncSessionLocal = None
