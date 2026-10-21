import json
import logging
import uuid
from collections.abc import Generator
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    Column,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    inspect,
)
from sqlalchemy.orm import Session, declarative_base, relationship, sessionmaker

try:
    from pgvector.sqlalchemy import Vector
except ImportError:
    Vector = None

try:
    from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
except ImportError:
    from sqlalchemy import JSON as JSONB
    TSVECTOR = Text

from .config import get_settings

settings = get_settings()


def utc_now():
    return datetime.now(UTC)


# Engine configuration
engine = create_engine(settings.database_url, pool_pre_ping=True)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def _owner_column() -> Column:
    """Owning account; every row of every data table belongs to exactly one user."""
    return Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)


class UserModel(Base):
    """Account that owns projects, meetings, topics and speaker turns."""
    __tablename__ = "users"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=utc_now, nullable=False)


class ProjectModel(Base):
    """Tier 1: Enterprise Project Entity."""
    __tablename__ = "projects"

    id = Column(String(100), primary_key=True, index=True)
    user_id = _owner_column()
    name = Column(String(255), nullable=False)
    status = Column(String(50), default="ACTIVE", nullable=False)
    created_at = Column(DateTime, default=utc_now, nullable=False)

    meetings = relationship("MeetingModel", back_populates="project", cascade="all, delete-orphan")


class MeetingModel(Base):
    """Tier 2: Ingested Meeting Entity (Level 3 Summary)."""
    __tablename__ = "meetings"

    id = Column(String(100), primary_key=True, index=True)
    user_id = _owner_column()
    project_id = Column(String(100), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True)
    title = Column(String(255), nullable=True)
    summary = Column(Text, nullable=True)
    content_hash = Column(String(64), nullable=True, index=True)
    date = Column(String(100), nullable=True)
    duration = Column(Float, nullable=True)
    status = Column(String(50), default="PROCESSED", nullable=False)
    created_at = Column(DateTime, default=utc_now, nullable=False)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    project = relationship("ProjectModel", back_populates="meetings")

    # Level 2 topic segments
    topics = relationship(
        "MeetingTopicModel",
        back_populates="meeting",
        cascade="all, delete-orphan",
        order_by="MeetingTopicModel.topic_index",
    )

    # Level 1 normalized speaker turns
    turns = relationship(
        "SpeakerTurnModel",
        back_populates="meeting",
        cascade="all, delete-orphan",
        order_by="SpeakerTurnModel.turn_number",
    )


class MeetingTopicModel(Base):
    """Tier 3: Level 2 Meeting Topic Segments."""
    __tablename__ = "meeting_topics"

    id = Column(String(100), primary_key=True, index=True)
    user_id = _owner_column()
    meeting_id = Column(String(100), ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False, index=True)
    topic_index = Column(Integer, nullable=False)
    title = Column(String(255), nullable=False)
    compiled_text = Column(Text, nullable=True)
    start_time = Column(Float, nullable=False)
    end_time = Column(Float, nullable=False)

    meeting = relationship("MeetingModel", back_populates="topics")
    turns = relationship("SpeakerTurnModel", back_populates="topic")


class SpeakerTurnModel(Base):
    """Tier 4: Level 1 Normalized Speaker Turns."""
    __tablename__ = "speaker_turns"

    turn_id = Column(String(150), primary_key=True, index=True)  # mtg_<id>_turn_<NNN>
    user_id = _owner_column()
    meeting_id = Column(String(100), ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False, index=True)
    topic_id = Column(String(100), ForeignKey("meeting_topics.id", ondelete="SET NULL"), nullable=True, index=True)
    turn_number = Column(Integer, nullable=False)
    speaker_id = Column(String(100), nullable=False)
    speaker_name = Column(String(255), nullable=False)
    text = Column(Text, nullable=False)
    start_time = Column(Float, nullable=False)
    end_time = Column(Float, nullable=False)
    duration = Column(Float, nullable=False)
    is_question = Column(Boolean, default=False, nullable=True)
    is_commitment = Column(Boolean, default=False, nullable=True)
    is_blocker = Column(Boolean, default=False, nullable=True)
    _sentence_indices = Column("sentence_indices", Text, default="[]", nullable=False)
    embedding = Column(Vector(1536), nullable=True) if Vector is not None else Column(Text, nullable=True)
    meta_data = Column("metadata", JSONB, nullable=True, default=dict)
    tsv = Column(
        TSVECTOR,
        Computed(
            "to_tsvector('english', coalesce(speaker_name, '') || ' ' || coalesce(text, ''))",
            persisted=True,
        ),
        nullable=True,
    )

    __table_args__ = (
        Index("idx_speaker_turns_tsv", "tsv", postgresql_using="gin"),
        Index(
            "idx_speaker_turns_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    meeting = relationship("MeetingModel", back_populates="turns")
    topic = relationship("MeetingTopicModel", back_populates="turns")

    @property
    def sentence_indices(self) -> list[int]:
        if not self._sentence_indices:
            return []
        try:
            return json.loads(self._sentence_indices)
        except Exception:
            return []

    @sentence_indices.setter
    def sentence_indices(self, val: list[int]):
        self._sentence_indices = json.dumps(val or [])


BASELINE_REVISION = "e0d6e74c6561"
MIGRATION_LOCK_KEY = 7_204_001  # arbitrary app-wide id for the migration advisory lock


def _is_unversioned_legacy_schema() -> bool:
    """True when the app tables already exist but Alembic has never recorded a version.

    That is a database created by the old create_all-based startup. Running the baseline
    migration on it would fail with "relation already exists".
    """
    from .schema_versioning import _get_comment_heads

    with engine.begin() as conn:
        inspector = inspect(conn)
        has_tables = inspector.has_table("projects")
        has_version_table = inspector.has_table("alembic_version")
        has_version = bool(_get_comment_heads(conn))
    return has_tables and not has_version and not has_version_table


def init_db() -> None:
    """Initialize database tables and run pending Alembic migrations."""
    # Ensure vector extension is enabled in PostgreSQL before table creation
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()
    except Exception:
        logging.getLogger(__name__).warning("Could not enable the pgvector extension.", exc_info=True)

    try:
        from pathlib import Path

        from alembic import command
        from alembic.config import Config
        from sqlalchemy import text

        from .schema_versioning import install_schema_comment_versioning

        install_schema_comment_versioning()
        alembic_ini_path = Path(__file__).resolve().parent.parent.parent / "alembic.ini"
        if not alembic_ini_path.exists():
            # create_all cannot alter existing tables, so never substitute it for migrations.
            raise RuntimeError(f"alembic.ini not found at {alembic_ini_path}; the image must ship alembic/ and alembic.ini.")
        alembic_cfg = Config(str(alembic_ini_path))
        # One replica migrates at a time; the others wait, then find nothing left to do.
        with engine.connect() as lock_conn:
            lock_conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": MIGRATION_LOCK_KEY})
            try:
                if _is_unversioned_legacy_schema():
                    # Tables built by the pre-Alembic startup code already match the baseline revision.
                    command.stamp(alembic_cfg, BASELINE_REVISION)
                command.upgrade(alembic_cfg, "head")
            finally:
                lock_conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_LOCK_KEY})
                lock_conn.commit()
    except Exception:
        logging.getLogger(__name__).exception("Database migration failed; refusing to start.")
        raise


def get_db() -> Generator[Session, None, None]:
    """Dependency providing database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
