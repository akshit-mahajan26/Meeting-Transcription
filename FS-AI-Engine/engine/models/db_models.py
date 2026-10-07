import uuid
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from sqlalchemy import (
    Column,
    String,
    Float,
    Integer,
    Text,
    DateTime,
    ForeignKey,
    JSON,
    Index,
)
from sqlalchemy.orm import declarative_base, relationship
from pgvector.sqlalchemy import Vector
from ..core.config import settings

Base = declarative_base()


def utc_now() -> datetime:
    """Return timezone-aware current UTC datetime."""
    return datetime.now(timezone.utc)


class Project(Base):
    """Tier 1: Enterprise Project Entity."""
    __tablename__ = "projects"

    id = Column(String(100), primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    # Relationships
    meetings = relationship("Meeting", back_populates="project", cascade="all, delete-orphan")


class Meeting(Base):
    """Tier 2: Meeting Entity (Holds Level 3 Executive Summary & Content Hash for Idempotency)."""
    __tablename__ = "meetings"

    id = Column(String(150), primary_key=True, index=True)
    project_id = Column(String(100), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True)
    title = Column(String(255), nullable=True)
    summary = Column(Text, nullable=True)  # Level 3 Executive Summary
    content_hash = Column(String(64), nullable=False, index=True)  # SHA-256 for idempotency
    status = Column(String(50), default="PROCESSED", nullable=False)
    date = Column(String(100), nullable=True)
    duration = Column(Float, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    # Relationships
    project = relationship("Project", back_populates="meetings")
    topics = relationship("MeetingTopic", back_populates="meeting", cascade="all, delete-orphan", order_by="MeetingTopic.topic_index")
    turns = relationship("MeetingTurn", back_populates="meeting", cascade="all, delete-orphan", order_by="MeetingTurn.turn_number")


class MeetingTopic(Base):
    """Tier 3: Level 2 Topic Segment (meeting_topics chapter block)."""
    __tablename__ = "meeting_topics"

    id = Column(String(150), primary_key=True, index=True, default=lambda: str(uuid.uuid4()))
    meeting_id = Column(String(150), ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False, index=True)
    topic_index = Column(Integer, nullable=False)
    title = Column(String(255), nullable=False)
    compiled_text = Column(Text, nullable=False)
    start_time = Column(Float, nullable=False)
    end_time = Column(Float, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    # Relationships
    meeting = relationship("Meeting", back_populates="topics")
    turns = relationship("MeetingTurn", back_populates="topic", cascade="all, delete-orphan")


class MeetingTurn(Base):
    """Tier 4: Level 1 Speaker Turn (meeting_turns with PGVector array & enterprise metadata)."""
    __tablename__ = "meeting_turns"

    id = Column(String(200), primary_key=True, index=True)  # [meeting_id]_turn_[sequence_number]
    meeting_id = Column(String(150), ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False, index=True)
    topic_id = Column(String(150), ForeignKey("meeting_topics.id", ondelete="CASCADE"), nullable=False, index=True)
    turn_number = Column(Integer, nullable=False)
    speaker = Column(String(255), nullable=False)
    speaker_id = Column(String(100), nullable=True)
    text = Column(Text, nullable=False)
    start_time = Column(Float, nullable=False)
    end_time = Column(Float, nullable=False)
    project_tag = Column(String(100), nullable=True, index=True)
    
    # High-dimensional vector column (dimension dynamically read from centralized settings)
    vector = Column(Vector(settings.embedding_dimensions), nullable=True)
    
    # Enterprise metadata JSON block (signals, sentence indices, aliases)
    meta_data = Column("meta_data", JSON, nullable=True, default=dict)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    # Relationships
    meeting = relationship("Meeting", back_populates="turns")
    topic = relationship("MeetingTopic", back_populates="turns")


# PGVector HNSW index definition helper
def get_hnsw_index():
    return Index(
        "idx_meeting_turns_vector_hnsw",
        MeetingTurn.vector,
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"vector": "vector_cosine_ops"},
    )
