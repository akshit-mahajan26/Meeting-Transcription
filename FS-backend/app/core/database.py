import json
from datetime import datetime, timezone
from typing import Generator, List, Optional
from sqlalchemy import create_engine, Column, String, Float, Integer, Text, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker, relationship, Session

try:
    from pgvector.sqlalchemy import Vector
except ImportError:
    Vector = None

try:
    from sqlalchemy.dialects.postgresql import JSONB
except ImportError:
    from sqlalchemy import JSON as JSONB

from .config import get_settings

settings = get_settings()


def utc_now():
    return datetime.now(timezone.utc)


# Engine configuration
engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if "sqlite" in settings.database_url else {},
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class ProjectModel(Base):
    """Tier 1: Enterprise Project Entity."""
    __tablename__ = "projects"

    id = Column(String(100), primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    status = Column(String(50), default="ACTIVE", nullable=False)
    created_at = Column(DateTime, default=utc_now, nullable=False)

    meetings = relationship("MeetingModel", back_populates="project", cascade="all, delete-orphan")


class MeetingModel(Base):
    """Tier 2: Ingested Meeting Entity (Level 3 Summary)."""
    __tablename__ = "meetings"

    id = Column(String(100), primary_key=True, index=True)
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

    meeting = relationship("MeetingModel", back_populates="turns")
    topic = relationship("MeetingTopicModel", back_populates="turns")

    @property
    def sentence_indices(self) -> List[int]:
        if not self._sentence_indices:
            return []
        try:
            return json.loads(self._sentence_indices)
        except Exception:
            return []

    @sentence_indices.setter
    def sentence_indices(self, val: List[int]):
        self._sentence_indices = json.dumps(val or [])


def init_db() -> None:
    """Initialize database tables for all 4 tiers and run lightweight auto-migrations."""
    # Ensure vector extension is enabled in PostgreSQL before table creation
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()
    except Exception:
        pass

    Base.metadata.create_all(bind=engine)

    # Lightweight auto-migration for newly added columns across tiers
    try:
        from sqlalchemy import text, inspect
        inspector = inspect(engine)
        
        if inspector.has_table("meetings"):
            existing_cols = {col["name"] for col in inspector.get_columns("meetings")}
            with engine.connect() as conn:
                if "project_id" not in existing_cols:
                    conn.execute(text("ALTER TABLE meetings ADD COLUMN project_id VARCHAR(100)"))
                if "summary" not in existing_cols:
                    conn.execute(text("ALTER TABLE meetings ADD COLUMN summary TEXT"))
                if "content_hash" not in existing_cols:
                    conn.execute(text("ALTER TABLE meetings ADD COLUMN content_hash VARCHAR(64)"))
                conn.commit()

        if inspector.has_table("speaker_turns"):
            existing_cols = {col["name"] for col in inspector.get_columns("speaker_turns")}
            with engine.connect() as conn:
                if "topic_id" not in existing_cols:
                    conn.execute(text("ALTER TABLE speaker_turns ADD COLUMN topic_id VARCHAR(100)"))
                if "metadata" not in existing_cols:
                    conn.execute(text("ALTER TABLE speaker_turns ADD COLUMN metadata JSONB"))
                if "embedding" not in existing_cols:
                    try:
                        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                        conn.execute(text("ALTER TABLE speaker_turns ADD COLUMN embedding vector(1536)"))
                    except Exception:
                        pass
                if "tsv" not in existing_cols:
                    try:
                        conn.execute(text(
                            "ALTER TABLE speaker_turns ADD COLUMN tsv tsvector "
                            "GENERATED ALWAYS AS ("
                            "to_tsvector('english', coalesce(speaker_name, '') || ' ' || coalesce(text, ''))"
                            ") STORED"
                        ))
                    except Exception:
                        pass
                try:
                    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_speaker_turns_tsv ON speaker_turns USING gin (tsv)"))
                    conn.execute(text("CREATE INDEX IF NOT EXISTS idx_speaker_turns_embedding_hnsw ON speaker_turns USING hnsw (embedding vector_cosine_ops)"))
                except Exception:
                    pass
                conn.commit()
    except Exception:
        pass


def get_db() -> Generator[Session, None, None]:
    """Dependency providing database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
