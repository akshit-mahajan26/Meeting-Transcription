"""knowledge graph initial schema

Creates kg_entities, kg_mentions and kg_relations in the knowledge graph database, with an HNSW index on the
entity embeddings for entity resolution and query-time entity lookup.

Revision ID: b7e2d4a91c03
Revises:
Create Date: 2026-11-04 12:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b7e2d4a91c03"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "kg_entities",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("entity_type", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=500), nullable=False),
        sa.Column("normalized_name", sa.String(length=500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("aliases", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.Column("mention_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "entity_type", "normalized_name", name="uq_kg_entities_user_type_name"),
    )
    op.create_index("ix_kg_entities_user_id", "kg_entities", ["user_id"])
    op.create_index(
        "idx_kg_entities_embedding_hnsw",
        "kg_entities",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )

    op.create_table(
        "kg_mentions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("entity_id", sa.String(length=36), nullable=False),
        sa.Column("meeting_id", sa.String(length=100), nullable=False),
        sa.Column("project_id", sa.String(length=100), nullable=True),
        sa.Column("turn_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.ForeignKeyConstraint(["entity_id"], ["kg_entities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("entity_id", "meeting_id", name="uq_kg_mentions_entity_meeting"),
    )
    op.create_index("ix_kg_mentions_user_id", "kg_mentions", ["user_id"])
    op.create_index("ix_kg_mentions_entity_id", "kg_mentions", ["entity_id"])
    op.create_index("idx_kg_mentions_user_meeting", "kg_mentions", ["user_id", "meeting_id"])

    op.create_table(
        "kg_relations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("source_id", sa.String(length=36), nullable=False),
        sa.Column("target_id", sa.String(length=36), nullable=False),
        sa.Column("relation_type", sa.String(length=60), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("meeting_id", sa.String(length=100), nullable=False),
        sa.Column("project_id", sa.String(length=100), nullable=True),
        sa.Column("turn_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["kg_entities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_id"], ["kg_entities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_kg_relations_user_id", "kg_relations", ["user_id"])
    op.create_index("ix_kg_relations_source_id", "kg_relations", ["source_id"])
    op.create_index("ix_kg_relations_target_id", "kg_relations", ["target_id"])
    op.create_index("idx_kg_relations_user_meeting", "kg_relations", ["user_id", "meeting_id"])


def downgrade() -> None:
    op.drop_table("kg_relations")
    op.drop_table("kg_mentions")
    op.drop_table("kg_entities")
