"""initial_schema

Revision ID: e0d6e74c6561
Revises:
Create Date: 2026-10-13 09:45:04.726569

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

try:
    from pgvector.sqlalchemy import Vector
except ImportError:
    Vector = None

# revision identifiers, used by Alembic.
revision: str = 'e0d6e74c6561'
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create initial baseline schema across all 4 tiers."""
    # Ensure vector extension is available in PostgreSQL
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # Tier 1: Projects
    op.create_table(
        'projects',
        sa.Column('id', sa.String(length=100), primary_key=True),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('status', sa.String(length=50), nullable=False, server_default='ACTIVE'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_projects_id', 'projects', ['id'], unique=False)

    # Tier 2: Meetings
    op.create_table(
        'meetings',
        sa.Column('id', sa.String(length=100), primary_key=True),
        sa.Column('project_id', sa.String(length=100), sa.ForeignKey('projects.id', ondelete='SET NULL'), nullable=True),
        sa.Column('title', sa.String(length=255), nullable=True),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('content_hash', sa.String(length=64), nullable=True),
        sa.Column('date', sa.String(length=100), nullable=True),
        sa.Column('duration', sa.Float(), nullable=True),
        sa.Column('status', sa.String(length=50), nullable=False, server_default='PROCESSED'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_meetings_id', 'meetings', ['id'], unique=False)
    op.create_index('ix_meetings_project_id', 'meetings', ['project_id'], unique=False)
    op.create_index('ix_meetings_content_hash', 'meetings', ['content_hash'], unique=False)

    # Tier 3: Meeting Topics
    op.create_table(
        'meeting_topics',
        sa.Column('id', sa.String(length=100), primary_key=True),
        sa.Column('meeting_id', sa.String(length=100), sa.ForeignKey('meetings.id', ondelete='CASCADE'), nullable=False),
        sa.Column('topic_index', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('compiled_text', sa.Text(), nullable=True),
        sa.Column('start_time', sa.Float(), nullable=False),
        sa.Column('end_time', sa.Float(), nullable=False),
    )
    op.create_index('ix_meeting_topics_id', 'meeting_topics', ['id'], unique=False)
    op.create_index('ix_meeting_topics_meeting_id', 'meeting_topics', ['meeting_id'], unique=False)

    # Tier 4: Speaker Turns
    embedding_col = sa.Column('embedding', Vector(1536), nullable=True) if Vector is not None else sa.Column('embedding', sa.Text(), nullable=True)
    op.create_table(
        'speaker_turns',
        sa.Column('turn_id', sa.String(length=150), primary_key=True),
        sa.Column('meeting_id', sa.String(length=100), sa.ForeignKey('meetings.id', ondelete='CASCADE'), nullable=False),
        sa.Column('topic_id', sa.String(length=100), sa.ForeignKey('meeting_topics.id', ondelete='SET NULL'), nullable=True),
        sa.Column('turn_number', sa.Integer(), nullable=False),
        sa.Column('speaker_id', sa.String(length=100), nullable=False),
        sa.Column('speaker_name', sa.String(length=255), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('start_time', sa.Float(), nullable=False),
        sa.Column('end_time', sa.Float(), nullable=False),
        sa.Column('duration', sa.Float(), nullable=False),
        sa.Column('is_question', sa.Boolean(), nullable=True, server_default=sa.text('false')),
        sa.Column('is_commitment', sa.Boolean(), nullable=True, server_default=sa.text('false')),
        sa.Column('is_blocker', sa.Boolean(), nullable=True, server_default=sa.text('false')),
        sa.Column('sentence_indices', sa.Text(), nullable=False, server_default='[]'),
        embedding_col,
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            'tsv',
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('english', coalesce(speaker_name, '') || ' ' || coalesce(text, ''))",
                persisted=True,
            ),
            nullable=True,
        ),
    )
    op.create_index('ix_speaker_turns_turn_id', 'speaker_turns', ['turn_id'], unique=False)
    op.create_index('ix_speaker_turns_meeting_id', 'speaker_turns', ['meeting_id'], unique=False)
    op.create_index('ix_speaker_turns_topic_id', 'speaker_turns', ['topic_id'], unique=False)
    op.create_index('idx_speaker_turns_tsv', 'speaker_turns', ['tsv'], unique=False, postgresql_using='gin')
    op.create_index(
        'idx_speaker_turns_embedding_hnsw',
        'speaker_turns',
        ['embedding'],
        unique=False,
        postgresql_using='hnsw',
        postgresql_ops={'embedding': 'vector_cosine_ops'},
    )


def downgrade() -> None:
    """Drop all tables in reverse order."""
    op.drop_index('idx_speaker_turns_embedding_hnsw', table_name='speaker_turns', postgresql_using='hnsw')
    op.drop_index('idx_speaker_turns_tsv', table_name='speaker_turns', postgresql_using='gin')
    op.drop_index('ix_speaker_turns_topic_id', table_name='speaker_turns')
    op.drop_index('ix_speaker_turns_meeting_id', table_name='speaker_turns')
    op.drop_index('ix_speaker_turns_turn_id', table_name='speaker_turns')
    op.drop_table('speaker_turns')

    op.drop_index('ix_meeting_topics_meeting_id', table_name='meeting_topics')
    op.drop_index('ix_meeting_topics_id', table_name='meeting_topics')
    op.drop_table('meeting_topics')

    op.drop_index('ix_meetings_content_hash', table_name='meetings')
    op.drop_index('ix_meetings_project_id', table_name='meetings')
    op.drop_index('ix_meetings_id', table_name='meetings')
    op.drop_table('meetings')

    op.drop_index('ix_projects_id', table_name='projects')
    op.drop_table('projects')
