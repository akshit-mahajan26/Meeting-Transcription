"""meetings.failure_kind: the exact cause of a failed ingestion (no new tables)

Lets the Ingest page show a precise reason and next step, and decides whether a failure is retried automatically
(temporary causes) or not (causes waiting cannot fix, such as a meeting without a transcript). Null for meetings that
never failed and for failures recorded before this column existed.

Revision ID: b3d7f1a26e58
Revises: a8c5e2d91f36
Create Date: 2026-11-28 16:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3d7f1a26e58"
down_revision: str | Sequence[str] | None = "a8c5e2d91f36"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Idempotent."""
    if "failure_kind" not in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("meetings")}:
        op.add_column("meetings", sa.Column("failure_kind", sa.String(40), nullable=True))


def downgrade() -> None:
    if "failure_kind" in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("meetings")}:
        op.drop_column("meetings", "failure_kind")
