"""ingestion queue on meetings and token cutoff on users (no new tables)

- meetings.ingest_requested_at: set when an ingestion is requested; the in-process runner processes rows where it
  is not null (the queue). Cleared when the ingestion finishes, successfully or not.
- meetings.ingest_claimed_at: set when a runner claims the row; a claim older than 30 minutes is considered
  abandoned (crashed process) and the row can be claimed again.
- users.tokens_valid_after: access tokens issued before this time are rejected (logout).

Revision ID: d7a9c3e15b42
Revises: c4e8a1f20b93
Create Date: 2026-11-27 18:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d7a9c3e15b42"
down_revision: str | Sequence[str] | None = "c4e8a1f20b93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_COLUMNS = (
    ("meetings", "ingest_requested_at"),
    ("meetings", "ingest_claimed_at"),
    ("users", "tokens_valid_after"),
)
QUEUE_INDEX = "ix_meetings_ingest_queue"


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    """Idempotent: each step checks before it acts."""
    bind = op.get_bind()
    for table, column in NEW_COLUMNS:
        if column not in _columns(bind, table):
            op.add_column(table, sa.Column(column, sa.DateTime(timezone=True), nullable=True))
    if QUEUE_INDEX not in {ix["name"] for ix in sa.inspect(bind).get_indexes("meetings")}:
        # Partial index: only queued rows are indexed, so it stays tiny
        op.create_index(
            QUEUE_INDEX, "meetings", ["ingest_requested_at"], postgresql_where=sa.text("ingest_requested_at IS NOT NULL")
        )


def downgrade() -> None:
    bind = op.get_bind()
    if QUEUE_INDEX in {ix["name"] for ix in sa.inspect(bind).get_indexes("meetings")}:
        op.drop_index(QUEUE_INDEX, table_name="meetings")
    for table, column in NEW_COLUMNS:
        if column in _columns(bind, table):
            op.drop_column(table, column)
