"""Knowledge graph database: entities and relations extracted from meetings.

Lives in its own database (KG_DATABASE_URL), so nothing here can reference users or meetings by foreign key.
Ownership is the user_id column on every row, and per-meeting cleanup is done by kg_service, not by cascades.
"""

import logging
import uuid
from collections.abc import Generator
from datetime import UTC, datetime

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import Session, declarative_base, sessionmaker

try:
    from pgvector.sqlalchemy import Vector
except ImportError:
    Vector = None

try:
    from sqlalchemy.dialects.postgresql import JSONB
except ImportError:
    from sqlalchemy import JSON as JSONB

from .config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

KG_MIGRATION_LOCK_KEY = 7_204_002  # advisory lock id for the knowledge graph migrations

kg_engine = create_engine(settings.kg_database_url, pool_pre_ping=True) if settings.kg_database_url else None
KGSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=kg_engine) if kg_engine is not None else None
KGBase = declarative_base()


def utc_now():
    return datetime.now(UTC)


def _new_id() -> str:
    return str(uuid.uuid4())


class KGEntityModel(KGBase):
    """A node: a person, project, decision, action item, blocker, technology, topic, organization or meeting."""
    __tablename__ = "kg_entities"

    id = Column(String(36), primary_key=True, default=_new_id)
    user_id = Column(String(36), nullable=False, index=True)
    entity_type = Column(String(40), nullable=False)
    name = Column(String(500), nullable=False)
    normalized_name = Column(String(500), nullable=False)
    description = Column(Text, nullable=True)
    aliases = Column(JSONB, nullable=False, default=list)
    embedding = Column(Vector(1536), nullable=True) if Vector is not None else Column(Text, nullable=True)
    mention_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=utc_now, nullable=False)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "entity_type", "normalized_name", name="uq_kg_entities_user_type_name"),
        Index(
            "idx_kg_entities_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class KGMentionModel(KGBase):
    """Where an entity came up: one row per entity per meeting, with the grounding turn ids."""
    __tablename__ = "kg_mentions"

    id = Column(String(36), primary_key=True, default=_new_id)
    user_id = Column(String(36), nullable=False, index=True)
    entity_id = Column(String(36), ForeignKey("kg_entities.id", ondelete="CASCADE"), nullable=False, index=True)
    meeting_id = Column(String(100), nullable=False)
    project_id = Column(String(100), nullable=True)
    turn_ids = Column(JSONB, nullable=False, default=list)

    __table_args__ = (
        UniqueConstraint("entity_id", "meeting_id", name="uq_kg_mentions_entity_meeting"),
        Index("idx_kg_mentions_user_meeting", "user_id", "meeting_id"),
    )


class KGRelationModel(KGBase):
    """A directed edge between two entities, stated in one meeting and grounded to its turns."""
    __tablename__ = "kg_relations"

    id = Column(String(36), primary_key=True, default=_new_id)
    user_id = Column(String(36), nullable=False, index=True)
    source_id = Column(String(36), ForeignKey("kg_entities.id", ondelete="CASCADE"), nullable=False, index=True)
    target_id = Column(String(36), ForeignKey("kg_entities.id", ondelete="CASCADE"), nullable=False, index=True)
    relation_type = Column(String(60), nullable=False)
    description = Column(Text, nullable=True)
    meeting_id = Column(String(100), nullable=False)
    project_id = Column(String(100), nullable=True)
    turn_ids = Column(JSONB, nullable=False, default=list)
    confidence = Column(Float, nullable=False, default=1.0)
    created_at = Column(DateTime, default=utc_now, nullable=False)

    __table_args__ = (Index("idx_kg_relations_user_meeting", "user_id", "meeting_id"),)


def init_kg_db() -> None:
    """Runs the knowledge graph's own Alembic chain (kg_alembic/). Skipped when KG_DATABASE_URL is not set."""
    if kg_engine is None:
        logger.warning("KG_DATABASE_URL is not configured; the knowledge graph is disabled.")
        return

    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text

    from .schema_versioning import install_schema_comment_versioning

    install_schema_comment_versioning()
    ini_path = Path(__file__).resolve().parent.parent.parent / "kg_alembic.ini"
    if not ini_path.exists():
        raise RuntimeError(f"kg_alembic.ini not found at {ini_path}; the image must ship kg_alembic/ and kg_alembic.ini.")
    cfg = Config(str(ini_path))
    with kg_engine.connect() as lock_conn:
        lock_conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": KG_MIGRATION_LOCK_KEY})
        try:
            command.upgrade(cfg, "head")
        finally:
            lock_conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": KG_MIGRATION_LOCK_KEY})
            lock_conn.commit()


def get_kg_db() -> Generator[Session | None, None, None]:
    """Dependency providing a knowledge graph session (None when the graph is not configured)."""
    if KGSessionLocal is None:
        yield None
        return
    db = KGSessionLocal()
    try:
        yield db
    finally:
        db.close()
