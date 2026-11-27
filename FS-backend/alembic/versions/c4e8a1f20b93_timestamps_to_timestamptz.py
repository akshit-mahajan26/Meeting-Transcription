"""convert timestamp columns to timestamptz

The application writes timezone-aware UTC datetimes, but these columns were `timestamp without time zone`,
so Postgres converted each value to the connection's TimeZone (for example Asia/Kolkata) before dropping the
zone. This migration converts every such column to `timestamp with time zone`, reading each stored value in
the database's current TimeZone, so the instant each row represents does not change.

It must run against the same database, with its TimeZone setting unchanged since the rows were written.
The TimeZone used is logged. Idempotent: columns that are already timestamptz are skipped.

Revision ID: c4e8a1f20b93
Revises: a3f1c9d27b40
Create Date: 2026-11-27 12:00:00.000000

"""
import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4e8a1f20b93"
down_revision: str | Sequence[str] | None = "a3f1c9d27b40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

TIMESTAMP_COLUMNS = (
    ("users", "created_at"),
    ("projects", "created_at"),
    ("meetings", "created_at"),
    ("meetings", "updated_at"),
)


def _has_timezone(bind, table: str, column: str) -> bool | None:
    """True/False for an existing column's timezone flag, None when the column does not exist."""
    for col in sa.inspect(bind).get_columns(table):
        if col["name"] == column:
            return bool(getattr(col["type"], "timezone", False))
    return None


def upgrade() -> None:
    bind = op.get_bind()
    tz = bind.execute(sa.text("SELECT current_setting('TimeZone')")).scalar()
    logger.info(f"Converting timestamp columns to timestamptz, reading stored values in TimeZone '{tz}'")
    for table, column in TIMESTAMP_COLUMNS:
        if _has_timezone(bind, table, column) is not False:
            continue  # missing, or already timestamptz
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN {column} TYPE TIMESTAMP WITH TIME ZONE "
            f"USING {column} AT TIME ZONE current_setting('TimeZone')"
        )


def downgrade() -> None:
    bind = op.get_bind()
    for table, column in TIMESTAMP_COLUMNS:
        if _has_timezone(bind, table, column) is not True:
            continue
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN {column} TYPE TIMESTAMP WITHOUT TIME ZONE "
            f"USING {column} AT TIME ZONE current_setting('TimeZone')"
        )
