"""In-process scheduled jobs (plan section 11): auto-sync, retries, key re-encryption, purge.

Every replica runs the loop; only the one holding the Postgres advisory lock on a dedicated connection acts.
"""

import asyncio
import logging
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from ..core import crypto
from ..core.database import SessionLocal, UserModel, settings
from . import fireflies_sync
from .ingest_runner import ingest_runner

logger = logging.getLogger(__name__)

LEADER_LOCK_KEY = 7_204_002  # distinct from MIGRATION_LOCK_KEY
TICK_SECONDS = 60
HOURLY_SECONDS = 3600
PURGE_BATCH = 50

DUE_SYNCS_SQL = text(
    "SELECT id FROM users WHERE fireflies_api_key_encrypted IS NOT NULL AND fireflies_auto_sync = true "
    "AND (fireflies_sync_heartbeat_at IS NULL OR fireflies_sync_heartbeat_at < now() - interval '60 minutes') "
    "AND (fireflies_last_sync_result IS NULL OR (fireflies_last_sync_result->>'finished_at') IS NULL "
    "OR (fireflies_last_sync_result->>'finished_at')::timestamptz < now() - make_interval(mins => :interval)) "
    "AND (fireflies_last_sync_result IS NULL OR fireflies_last_sync_result->>'outcome' IS DISTINCT FROM 'stopped_rate_limited' "
    "OR now() > COALESCE((fireflies_last_sync_result->>'retry_after')::timestamptz, "
    "(fireflies_last_sync_result->>'finished_at')::timestamptz + make_interval(mins => :interval)))"
)

RETRIES_SQL = text(
    "UPDATE meetings SET ingest_requested_at = now(), updated_at = now() WHERE id IN ("
    "SELECT id FROM meetings WHERE status = 'FAILED' AND retry_until > now() AND updated_at < now() - interval '1 hour' "
    "AND ingest_requested_at IS NULL ORDER BY updated_at LIMIT 20 FOR UPDATE SKIP LOCKED)"
)

PURGE_SQL = text(
    f"DELETE FROM meetings WHERE id IN (SELECT id FROM meetings WHERE purge_at <= now() LIMIT {PURGE_BATCH})"
)


def due_user_ids() -> list[str]:
    with SessionLocal() as db:
        return [r[0] for r in db.execute(DUE_SYNCS_SQL, {"interval": settings.fireflies_auto_sync_interval_minutes})]


def request_retries() -> int:
    with SessionLocal() as db:
        count = db.execute(RETRIES_SQL).rowcount
        db.commit()
        return int(count or 0)


def reencrypt_all() -> tuple[int, int]:
    """Returns (re-encrypted, skipped because undecryptable)."""
    done = skipped = 0
    with SessionLocal() as db:
        users = db.query(UserModel.id, UserModel.fireflies_api_key_encrypted).filter(
            UserModel.fireflies_api_key_encrypted.isnot(None)
        ).all()
    for user_id, token in users:
        try:
            if not crypto.needs_reencryption(token):
                continue
            new_token = crypto.reencrypt_secret(token)
        except crypto.SecretDecryptionError:
            skipped += 1
            continue
        with SessionLocal() as db:
            db.query(UserModel).filter(UserModel.id == user_id).update({"fireflies_api_key_encrypted": new_token})
            db.commit()
        done += 1
    return done, skipped


def purge_batch() -> int:
    with SessionLocal() as db:
        count = db.execute(PURGE_SQL).rowcount
        db.commit()
        return int(count or 0)


class Scheduler:
    def __init__(self) -> None:
        self._loop_task: asyncio.Task | None = None
        self._conn: Any = None
        self._leader = False
        self._jobs: dict[str, asyncio.Task] = {}
        self._last_hourly = 0.0
        self._hourly_ran = False

    def start(self) -> None:
        if self._loop_task is None:
            self._loop_task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        tasks = [t for t in [self._loop_task, *self._jobs.values()] if t is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._loop_task, self._jobs, self._leader = None, {}, False
        await asyncio.to_thread(self._close_conn)

    def _close_conn(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()  # closing the session releases the advisory lock
            except Exception:
                logger.warning("Could not close the scheduler leader connection", exc_info=True)

    def _try_lock(self) -> bool:
        """Open the dedicated connection if needed and try the lock; a broken connection drops leadership."""
        try:
            if self._conn is None:
                self._conn = create_engine(settings.database_url, poolclass=NullPool).connect()
            got = bool(self._conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LEADER_LOCK_KEY}).scalar())
            self._conn.commit()
            return got
        except Exception:
            logger.warning("Scheduler leader check failed", exc_info=True)
            self._close_conn()
            return False

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("Scheduler tick failed", exc_info=True)
            await asyncio.sleep(TICK_SECONDS)

    async def tick(self) -> None:
        if not self._leader:
            self._leader = await asyncio.to_thread(self._try_lock)
            if self._leader:
                logger.info("Scheduler: this process is now the leader")
        if not self._leader:
            return
        loop = asyncio.get_running_loop()
        if not self._hourly_ran or loop.time() - self._last_hourly >= HOURLY_SECONDS:
            self._hourly_ran, self._last_hourly = True, loop.time()
            self._start_job("reencrypt_keys", self._reencrypt_job)
            self._start_job("purge_meetings", self._purge_job)
        for step in (self.start_due_syncs, self.request_due_retries):
            try:
                await step()
            except Exception:
                logger.error("Scheduler job %s failed", step.__name__, exc_info=True)

    def _start_job(self, name: str, factory) -> None:
        task = self._jobs.get(name)
        if task is not None and not task.done():
            return
        self._jobs[name] = asyncio.create_task(factory())

    async def start_due_syncs(self) -> None:
        if not crypto.linking_configured():
            return
        for user_id in await asyncio.to_thread(due_user_ids):
            await fireflies_sync.start_sync(user_id)

    async def request_due_retries(self) -> None:
        if await asyncio.to_thread(request_retries):
            ingest_runner.wake()

    async def _reencrypt_job(self) -> None:
        try:
            done, skipped = await asyncio.to_thread(reencrypt_all)
            logger.info("Scheduler: re-encrypted %s Fireflies keys, skipped %s", done, skipped)
        except Exception:
            logger.error("Scheduler re-encryption failed", exc_info=True)

    async def _purge_job(self) -> None:
        try:
            total = 0
            while (n := await asyncio.to_thread(purge_batch)) > 0:
                total += n
            logger.info("Scheduler: purged %s meetings", total)
        except Exception:
            logger.error("Scheduler purge failed", exc_info=True)


scheduler = Scheduler()
