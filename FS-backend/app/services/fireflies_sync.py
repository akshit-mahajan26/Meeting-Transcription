"""Fireflies account sync (plan section 9): lists the linked account's meetings and queues new or changed ones.

The sync never processes meetings; the ingest runner does. All DB work runs in short sessions via asyncio.to_thread.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from ..core import crypto
from ..core.config import get_settings
from ..core.database import MeetingModel, SessionLocal, UserModel
from . import failure_kinds, ownership
from .fireflies_client import FirefliesAPIError, FirefliesClient, classify_fireflies_error
from .ingest_runner import ingest_runner
from .pipeline_orchestrator import BackendPipelineOrchestrator

logger = logging.getLogger(__name__)

PAGE_SIZE = 10
FALLBACK_PAGE_SIZE = 5  # a page that timed out is re-requested with this limit, so a short page of this size is not the end
MAX_SKIPPED_LISTED = 50

_tasks: set[asyncio.Task] = set()  # strong references so running syncs are not garbage collected

_CLAIM_SQL = text(
    "UPDATE users SET fireflies_sync_heartbeat_at = now() WHERE id = :user AND fireflies_api_key_encrypted IS NOT NULL "
    "AND (fireflies_sync_heartbeat_at IS NULL OR fireflies_sync_heartbeat_at < now() - interval '60 minutes')"
)


# ------------------------------------------------------------------------------
# Short synchronous DB helpers (each opens and closes its own session)
# ------------------------------------------------------------------------------

def _claim(user_id: str) -> bool:
    with SessionLocal() as db:
        updated = db.execute(_CLAIM_SQL, {"user": user_id}).rowcount
        db.commit()
        return bool(updated)


def _load_user(user_id: str) -> dict[str, Any] | None:
    with SessionLocal() as db:
        user = db.get(UserModel, user_id)
        if user is None or not user.fireflies_api_key_encrypted:
            return None
        return {
            "token": user.fireflies_api_key_encrypted,
            "project_id": user.fireflies_sync_project_id,
            "last_synced_at": user.fireflies_last_synced_at,
            "last_full_sync_at": user.fireflies_last_full_sync_at,
        }


def _save_key(user_id: str, token: str) -> None:
    with SessionLocal() as db:
        db.query(UserModel).filter(UserModel.id == user_id).update({"fireflies_api_key_encrypted": token})
        db.commit()


def _process_transcript(
    user_id: str, meeting_id: str, title: str, content_hash: str, project_id: str | None, include_deleted: bool = False
) -> str:
    """Steps 4.1 to 4.4 and 4.6 for one transcript.

    Returns disconnected, other_account, restored, new_or_updated or unchanged.
    """
    with SessionLocal() as db:
        linked = db.query(UserModel.id).filter(
            UserModel.id == user_id, UserModel.fireflies_api_key_encrypted.isnot(None)
        ).first()
        if linked is None:
            return "disconnected"
        if ownership.meeting_taken(db, user_id, meeting_id):
            return "other_account"
        meeting = db.get(MeetingModel, meeting_id)
        if meeting is not None and meeting.excluded_at is not None:
            outcome = "unchanged"  # deleted by the user: no change, but still seen
            meeting.purge_at = None  # still in Fireflies: keep the marker, or a purge would let a later sync re-add it
            if include_deleted:  # opt-in manual sync: bring the deleted meeting back (plan section 25)
                meeting.excluded_at = None
                meeting.status = "QUEUED"
                meeting.ingest_requested_at = db.execute(text("SELECT now()")).scalar()
                meeting.failure_kind = meeting.retry_until = meeting.summary = meeting.content_hash = None
                meeting.purge_at = None
                outcome = "restored"
            db.execute(text("UPDATE users SET fireflies_sync_heartbeat_at = now() WHERE id = :user"), {"user": user_id})
            db.commit()
            return outcome
        now = db.execute(text("SELECT now()")).scalar()
        if meeting is not None:
            meeting.purge_at = None
        if meeting is None:
            db.add(MeetingModel(
                id=meeting_id, user_id=user_id, title=(title or "")[:255], status="QUEUED", source="fireflies_sync",
                project_id=project_id, ingest_requested_at=now,
            ))
            outcome = "new_or_updated"
        elif meeting.ingest_requested_at is not None or (
            meeting.status == "PROCESSED" and meeting.content_hash == content_hash
        ):
            outcome = "unchanged"
        elif (
            meeting.status == "FAILED" and meeting.content_hash == content_hash
            and (k := failure_kinds.kind(meeting.failure_kind)) is not None and not k.retryable
        ):
            outcome = "unchanged"  # a permanent failure whose content did not change is not queued again
        else:
            meeting.ingest_requested_at = now
            outcome = "new_or_updated"
        db.execute(
            text("UPDATE users SET fireflies_sync_heartbeat_at = now() WHERE id = :user"), {"user": user_id}
        )
        db.commit()
        return outcome


def _reconcile(user_id: str, seen_ids: list[str]) -> int:
    """Step 6: schedule synced meetings missing from a completed full listing for deletion in 7 days."""
    with SessionLocal() as db:
        removed = db.execute(
            text(
                "UPDATE meetings SET purge_at = now() + interval '7 days' WHERE user_id = :u "
                "AND source = 'fireflies_sync' AND purge_at IS NULL AND id <> ALL(:seen)"
            ),
            {"u": user_id, "seen": seen_ids},
        ).rowcount
        db.commit()
        return int(removed or 0)


def _finish(user_id: str, result: dict[str, Any], cursors: datetime | None, full_sync: bool, auto_sync_off: bool) -> None:
    """Step 8: clear the heartbeat, store the result and, when completed, move the cursors."""
    with SessionLocal() as db:
        values: dict[str, Any] = {"fireflies_sync_heartbeat_at": None, "fireflies_last_sync_result": result}
        if cursors is not None:
            values["fireflies_last_synced_at"] = cursors
            if full_sync:
                values["fireflies_last_full_sync_at"] = cursors
        if auto_sync_off:
            values["fireflies_auto_sync"] = False
        db.query(UserModel).filter(UserModel.id == user_id).update(values)
        db.commit()


# ------------------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------------------

async def start_sync(user_id: str, include_deleted: bool = False) -> bool:
    """Claim the account's sync slot; when claimed, run the sync as a background task. False = not linked or running."""
    if not await asyncio.to_thread(_claim, user_id):
        return False
    task = asyncio.create_task(run_sync(user_id, True) if include_deleted else run_sync(user_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return True


async def cancel_all_syncs() -> None:
    """Cancel running syncs and wait for them; their finally blocks clear the heartbeat."""
    tasks = list(_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def run_sync(user_id: str, include_deleted: bool = False) -> None:
    """Steps 2 to 5, 7 and 8 of the sync algorithm. The caller has already made the claim."""
    settings = get_settings()
    run_started = datetime.now(UTC)
    result: dict[str, Any] = {
        "started_at": run_started.isoformat(), "finished_at": None, "outcome": "stopped_error",
        "new_or_updated": 0, "restored": 0, "unchanged": 0, "skipped_other_account": 0, "skipped_other_account_meetings": [],
        "error": None, "retry_after": None, "full_sync": False, "removed_from_fireflies": 0,
    }
    full_sync = False
    auto_sync_off = False
    try:
        user = await asyncio.to_thread(_load_user, user_id)
        if user is None:
            result["outcome"] = "stopped_disconnected"
            return
        try:
            token = crypto.decrypt_secret(user["token"])
        except crypto.SecretDecryptionError:
            result["outcome"] = "stopped_auth_error"
            result["error"] = "The stored Fireflies key could not be read. Reconnect Fireflies with a new key."
            return
        if crypto.needs_reencryption(user["token"]):
            await asyncio.to_thread(_save_key, user_id, crypto.reencrypt_secret(user["token"]))
        client = FirefliesClient(api_key=token)

        last_full = user["last_full_sync_at"]
        full_sync = (
            include_deleted  # bringing deleted meetings back needs every meeting listed, not only recent ones
            or last_full is None
            or user["last_synced_at"] is None
            or last_full < run_started - timedelta(days=settings.fireflies_full_sync_days)
        )
        result["full_sync"] = full_sync
        from_date = None if full_sync else user["last_synced_at"] - timedelta(hours=24)
        seen_ids: set[str] = set()
        result["outcome"] = await _page_loop(
            user_id, client, user["project_id"], from_date, run_started, result, settings.fireflies_sync_max_meetings,
            seen_ids, include_deleted,
        )
        if full_sync and result["outcome"] == "completed":
            result["removed_from_fireflies"] = await asyncio.to_thread(_reconcile, user_id, sorted(seen_ids))
    except FirefliesAPIError as exc:
        kind = classify_fireflies_error(exc)
        if kind == "rate_limited":
            result["outcome"] = "stopped_rate_limited"
            result["retry_after"] = exc.retry_after.isoformat() if exc.retry_after else None
        elif kind in ("key_rejected", "no_access"):
            result["outcome"], auto_sync_off = "stopped_auth_error", True
            result["error"] = "Fireflies rejected the API key. Reconnect Fireflies with a new key."
        elif kind == "account_problem":
            result["outcome"], auto_sync_off = "stopped_account_error", True
            result["error"] = exc.message
        else:
            result["outcome"] = "stopped_error"
            result["error"] = "The sync stopped because of an unexpected Fireflies error."
            logger.error("Fireflies sync for user %s failed (%s, code=%s)", user_id, kind, exc.code, exc_info=True)
    except asyncio.CancelledError:
        result["outcome"] = "stopped_error"
        result["error"] = "The sync was interrupted by a backend restart."
        raise
    except Exception:
        result["outcome"] = "stopped_error"
        result["error"] = "The sync stopped because of an unexpected error."
        logger.error("Fireflies sync for user %s failed", user_id, exc_info=True)
    finally:
        result["finished_at"] = datetime.now(UTC).isoformat()
        completed = result["outcome"] == "completed"
        try:
            await asyncio.to_thread(_finish, user_id, result, run_started if completed else None, full_sync, auto_sync_off)
        except Exception:
            logger.error("Could not write the sync result for user %s", user_id, exc_info=True)
        logger.info(
            "Fireflies sync for user %s finished: outcome=%s queued=%s unchanged=%s",
            user_id, result["outcome"], result["new_or_updated"], result["unchanged"],
        )


async def _page_loop(
    user_id: str,
    client: FirefliesClient,
    project_id: str | None,
    from_date: datetime | None,
    to_date: datetime,
    result: dict[str, Any],
    cap: int,
    seen_ids: set[str] | None = None,
    include_deleted: bool = False,
) -> str:
    """Step 4: page through the listing and queue transcripts. Returns the outcome."""
    skip = 0
    while True:
        page = await client.list_transcripts(limit=PAGE_SIZE, skip=skip, from_date=from_date, to_date=to_date)
        for item in page:
            if seen_ids is not None:
                seen_ids.add(item["id"])
            content_hash = BackendPipelineOrchestrator.compute_transcript_hash(item.get("sentences") or [])
            extra = (True,) if include_deleted else ()
            outcome = await asyncio.to_thread(
                _process_transcript, user_id, item["id"], item.get("title") or "", content_hash, project_id, *extra
            )
            if outcome == "disconnected":
                return "stopped_disconnected"
            if outcome == "other_account":
                result["skipped_other_account"] += 1
                if len(result["skipped_other_account_meetings"]) < MAX_SKIPPED_LISTED:
                    result["skipped_other_account_meetings"].append({"id": item["id"], "title": item.get("title")})
                continue
            result[outcome] += 1
            if outcome == "restored":
                result["new_or_updated"] += 1
            if cap > 0 and result["new_or_updated"] >= cap:
                ingest_runner.wake()
                return "stopped_meeting_cap"
        ingest_runner.wake()
        skip += len(page)
        if not page or (len(page) < PAGE_SIZE and len(page) != FALLBACK_PAGE_SIZE):
            return "completed"
