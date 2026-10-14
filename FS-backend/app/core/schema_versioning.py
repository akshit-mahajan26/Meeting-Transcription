"""PostgreSQL Schema-Comment-based version tracker for Alembic (Zero-Table Migration).

Stores migration revision heads inside PostgreSQL metadata (COMMENT ON SCHEMA public)
instead of creating or maintaining an auxiliary 'alembic_version' table in the database.
"""

from typing import Any

from alembic.runtime.migration import HeadMaintainer, MigrationContext
from sqlalchemy import text

SCHEMA_NAME = "public"
COMMENT_PREFIX = "alembic_version:"

_is_patched = False


def _get_comment_heads(connection: Any) -> tuple[str, ...]:
    """Retrieve revision heads stored in the PostgreSQL schema comment."""
    if connection is None:
        return ()
    try:
        sql = text(
            "SELECT obj_description(oid, 'pg_namespace') "
            "FROM pg_namespace WHERE nspname = :schema_name"
        )
        res = connection.execute(sql, {"schema_name": SCHEMA_NAME}).scalar()
        if res and res.startswith(COMMENT_PREFIX):
            rev_str = res[len(COMMENT_PREFIX) :].strip()
            return tuple(r for r in rev_str.split(",") if r)
    except Exception:
        pass
    return ()


def _sync_schema_comment(context: Any, heads: set[str]) -> None:
    """Update or clear the PostgreSQL schema comment with the active revision heads."""
    if heads:
        val = f"{COMMENT_PREFIX}{','.join(sorted(heads))}"
        sql = f"COMMENT ON SCHEMA {SCHEMA_NAME} IS '{val}'"
    else:
        sql = f"COMMENT ON SCHEMA {SCHEMA_NAME} IS NULL"
    context.impl._exec(text(sql))


def _migrate_existing_table(connection: Any, impl: Any) -> None:
    """If an alembic_version table exists, migrate its version to the comment and drop the table."""
    if connection is None:
        return
    try:
        check_table = connection.execute(
            text(
                "SELECT EXISTS (SELECT FROM information_schema.tables "
                "WHERE table_schema = :schema_name AND table_name = 'alembic_version')"
            ),
            {"schema_name": SCHEMA_NAME},
        ).scalar()

        if check_table:
            row = connection.execute(
                text(f"SELECT version_num FROM {SCHEMA_NAME}.alembic_version LIMIT 1")
            ).scalar()
            if row:
                existing = _get_comment_heads(connection)
                if not existing:
                    val = f"{COMMENT_PREFIX}{row}"
                    impl._exec(text(f"COMMENT ON SCHEMA {SCHEMA_NAME} IS '{val}'"))
            impl._exec(text(f"DROP TABLE IF EXISTS {SCHEMA_NAME}.alembic_version"))
    except Exception:
        pass


def install_schema_comment_versioning() -> None:
    """Monkey-patch Alembic's MigrationContext and HeadMaintainer for PostgreSQL."""
    global _is_patched
    if _is_patched:
        return

    orig_get_heads = MigrationContext.get_current_heads
    orig_has_table = MigrationContext._has_version_table
    orig_ensure_table = MigrationContext._ensure_version_table
    orig_insert = HeadMaintainer._insert_version
    orig_update = HeadMaintainer._update_version
    orig_delete = HeadMaintainer._delete_version

    def patched_has_version_table(self: MigrationContext) -> bool:
        if self.dialect.name == "postgresql":
            if self.as_sql:
                return True
            heads = self.get_current_heads()
            return len(heads) > 0
        return orig_has_table(self)

    def patched_get_current_heads(self: MigrationContext) -> tuple[str, ...]:
        if self.as_sql:
            return orig_get_heads(self)

        if self.dialect.name == "postgresql":
            _migrate_existing_table(self.connection, self.impl)
            return _get_comment_heads(self.connection)

        return orig_get_heads(self)

    def patched_ensure_version_table(self: MigrationContext, purge: bool = False) -> None:
        if self.dialect.name == "postgresql":
            if purge:
                _sync_schema_comment(self, set())
            # Do not create any table in PostgreSQL
            return
        orig_ensure_table(self, purge=purge)

    def patched_insert_version(self: HeadMaintainer, version: str) -> None:
        if self.context.dialect.name == "postgresql":
            self.heads.add(version)
            _sync_schema_comment(self.context, self.heads)
        else:
            orig_insert(self, version)

    def patched_update_version(self: HeadMaintainer, from_: str, to_: str) -> None:
        if self.context.dialect.name == "postgresql":
            self.heads.discard(from_)
            self.heads.add(to_)
            _sync_schema_comment(self.context, self.heads)
        else:
            orig_update(self, from_, to_)

    def patched_delete_version(self: HeadMaintainer, version: str) -> None:
        if self.context.dialect.name == "postgresql":
            self.heads.discard(version)
            _sync_schema_comment(self.context, self.heads)
        else:
            orig_delete(self, version)

    MigrationContext._has_version_table = patched_has_version_table
    MigrationContext.get_current_heads = patched_get_current_heads
    MigrationContext._ensure_version_table = patched_ensure_version_table
    HeadMaintainer._insert_version = patched_insert_version
    HeadMaintainer._update_version = patched_update_version
    HeadMaintainer._delete_version = patched_delete_version

    _is_patched = True
