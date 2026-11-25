"""Alembic environment of the knowledge graph database (KG_DATABASE_URL), separate from the main chain in alembic/."""

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app.core.schema_versioning import install_schema_comment_versioning  # noqa: E402

install_schema_comment_versioning()

from app.core.config import get_settings  # noqa: E402
from app.core.kg_database import KGBase, kg_engine  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = KGBase.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    context.configure(
        url=get_settings().kg_database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    if kg_engine is None:
        raise RuntimeError("KG_DATABASE_URL is not configured.")
    with kg_engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
