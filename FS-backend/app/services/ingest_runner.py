"""In-process ingestion runner. The queue is the `meetings` table (rows with ingest_requested_at set);
rows are claimed atomically (FOR UPDATE SKIP LOCKED), which also serialises work per meeting across replicas."""

import asyncio
import contextlib
import logging
from typing import Any

from sqlalchemy import text

from ..core.config import get_settings
from ..core.database import SessionLocal
from .jobs import run_fireflies_ingest, run_raw_media_upload

logger = logging.getLogger(__name__)

POLL_SECONDS = 30
SHUTDOWN_GRACE_SECONDS = 60

CLAIM_SQL = text(
    "UPDATE meetings SET ingest_claimed_at = now() WHERE id IN ("
    "SELECT id FROM meetings WHERE ingest_requested_at IS NOT NULL "
    "AND (ingest_claimed_at IS NULL OR ingest_claimed_at < now() - interval '30 minutes') "
    "ORDER BY ingest_requested_at FOR UPDATE SKIP LOCKED LIMIT :free) "
    "RETURNING id, user_id, project_id, title"
)
CLEAR_SQL = text("UPDATE meetings SET ingest_requested_at = NULL, ingest_claimed_at = NULL WHERE id = :id")


def claim_rows(free: int) -> list[dict[str, Any]]:
    """Atomically claims up to `free` queued meetings."""
    db = SessionLocal()
    try:
        rows = db.execute(CLAIM_SQL, {"free": free}).mappings().all()
        db.commit()
        return [dict(r) for r in rows]
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def clear_row(meeting_id: str) -> None:
    """Removes a meeting from the queue (done or failed)."""
    db = SessionLocal()
    try:
        db.execute(CLEAR_SQL, {"id": meeting_id})
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


class IngestRunner:
    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()
        self._loop_task: asyncio.Task | None = None
        self._wake: asyncio.Event | None = None
        self._stopping = False
        self._limit = 1
        self._media_slots: asyncio.Semaphore | None = None

    async def start(self) -> None:
        if self._loop_task is not None:
            return
        self._limit = max(1, get_settings().ingest_max_concurrency)
        self._media_slots = asyncio.Semaphore(self._limit)
        self._wake = asyncio.Event()
        self._stopping = False
        self._loop_task = asyncio.create_task(self._run())
        logger.info(f"[Ingest Runner] Started (max concurrency {self._limit}).")

    def wake(self) -> None:
        if self._wake is not None:
            self._wake.set()

    def submit_media(self, media_url: str, title: str | None) -> None:
        """Best-effort (not durable) raw media upload, bounded by the concurrency limit."""
        if self._media_slots is None or self._stopping:
            logger.warning("[Ingest Runner] Not running; raw media upload dropped.")
            return
        self._track(asyncio.create_task(self._media(media_url, title)))

    async def _media(self, media_url: str, title: str | None) -> None:
        async with self._media_slots:
            await run_raw_media_upload(media_url, title)

    def _track(self, task: asyncio.Task) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        self.wake()  # a slot is free

    async def _run(self) -> None:
        while not self._stopping:
            try:
                free = self._limit - len(self._tasks)
                claimed = await asyncio.to_thread(claim_rows, free) if free > 0 else []
                for row in claimed:
                    task = asyncio.create_task(self._process(row))
                    self._track(task)
                if claimed:
                    logger.info(f"[Ingest Runner] Claimed {len(claimed)} meeting(s).")
                if len(claimed) == free and free > 0:
                    continue  # slots were full of work; check again once one frees up
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[Ingest Runner] Claim failed; will retry.")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), POLL_SECONDS)
            self._wake.clear()

    async def _process(self, row: dict[str, Any]) -> None:
        meeting_id = row["id"]
        try:
            await run_fireflies_ingest(row["user_id"], meeting_id, row.get("project_id"), row.get("title"))
        except asyncio.CancelledError:
            raise  # shutdown: keep the claim; it becomes reclaimable after 30 minutes
        except Exception:
            logger.exception(f"[Ingest Runner] Ingest of '{meeting_id}' raised unexpectedly.")
        try:
            await asyncio.to_thread(clear_row, meeting_id)
        except Exception:
            logger.exception(f"[Ingest Runner] Could not clear queue entry for '{meeting_id}'.")

    async def stop(self) -> None:
        self._stopping = True
        if self._loop_task is not None:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
            self._loop_task = None
        tasks = list(self._tasks)
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=SHUTDOWN_GRACE_SECONDS)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
                logger.warning(f"[Ingest Runner] Cancelled {len(pending)} unfinished task(s) on shutdown.")
        self._wake = None
        self._media_slots = None
        logger.info("[Ingest Runner] Stopped.")


ingest_runner = IngestRunner()
