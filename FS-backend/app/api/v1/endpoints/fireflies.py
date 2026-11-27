"""Fireflies account linking (plan section 8): connect, status, settings, manual sync, disconnect."""

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import text
from sqlalchemy.orm import Session

from ....api.deps import get_current_user
from ....core import crypto
from ....core.database import UserModel, get_db
from ....core.ratelimit import limit_by_user
from ....core.security import verify_password
from ....schemas.fireflies import (
    ConnectRequest,
    DisconnectRequest,
    DisconnectResponse,
    PendingDeletion,
    SettingsRequest,
    StatusResponse,
    SyncRequest,
)
from ....services.fireflies_client import FirefliesAPIError, FirefliesClient, classify_fireflies_error
from ....services.fireflies_sync import start_sync
from ....services.ownership import claim_project, unscope_project_id

logger = logging.getLogger(__name__)
router = APIRouter()


def _error(code: int, detail: str, headers: dict[str, str] | None = None) -> HTTPException:
    return HTTPException(status_code=code, detail=detail, headers=headers)


def _require_configured() -> None:
    if not crypto.linking_configured():
        raise _error(status.HTTP_503_SERVICE_UNAVAILABLE, "Fireflies account linking is not configured on this server.")


def _require_linked(user: UserModel) -> None:
    if not user.fireflies_api_key_encrypted:
        raise _error(status.HTTP_409_CONFLICT, "No Fireflies account is linked.")


async def _check_password(user: UserModel, password: str) -> None:
    # 403, not 401: the frontend logs the user out on any 401.
    if not await run_in_threadpool(verify_password, password, user.password_hash):
        raise _error(status.HTTP_403_FORBIDDEN, "Incorrect password.")


def _status_sync(db: Session, user: UserModel) -> StatusResponse:
    running = db.execute(
        text(
            "SELECT COALESCE(fireflies_sync_heartbeat_at > now() - interval '60 minutes', false) "
            "FROM users WHERE id = :u"
        ),
        {"u": user.id},
    ).scalar()
    count, earliest = db.execute(
        text("SELECT count(*), min(purge_at) FROM meetings WHERE user_id = :u AND purge_at IS NOT NULL"),
        {"u": user.id},
    ).one()
    deleted = db.execute(
        text("SELECT count(*) FROM meetings WHERE user_id = :u AND excluded_at IS NOT NULL"), {"u": user.id}
    ).scalar()
    return StatusResponse(
        linked=bool(user.fireflies_api_key_encrypted),
        account_email=user.fireflies_account_email,
        auto_sync=bool(user.fireflies_auto_sync),
        sync_project=unscope_project_id(user.fireflies_sync_project_id, user.id),
        sync_running=bool(running),
        sync_heartbeat_at=user.fireflies_sync_heartbeat_at,
        last_synced_at=user.fireflies_last_synced_at,
        last_sync_result=user.fireflies_last_sync_result,
        pending_deletion=PendingDeletion(meetings=count, purge_at=earliest) if count else None,
        deleted_meetings=int(deleted or 0),
    )


async def _status(db: Session, user: UserModel) -> StatusResponse:
    return await run_in_threadpool(_status_sync, db, user)


def _project_id(db: Session, user: UserModel, name: str | None) -> str | None:
    clean = (name or "").strip()
    return claim_project(db, user.id, clean).id if clean else None


def _store_link(db: Session, user: UserModel, key: str, email: str | None, sync_project: str | None) -> None:
    user.fireflies_api_key_encrypted = crypto.encrypt_secret(key)
    user.fireflies_account_email = email
    user.fireflies_auto_sync = False
    user.fireflies_sync_project_id = _project_id(db, user, sync_project)
    # The first sync after connecting must be a FULL sync: it clears the deletion dates a disconnect set on meetings
    # that are still in Fireflies (plan section 12). Resetting the cursors makes run_sync choose a full sync.
    user.fireflies_last_synced_at = None
    user.fireflies_last_full_sync_at = None
    db.commit()


def _apply_settings(db: Session, user: UserModel, payload: SettingsRequest) -> None:
    sent = payload.model_fields_set
    if "auto_sync" in sent and payload.auto_sync is not None:
        user.fireflies_auto_sync = payload.auto_sync
    if "sync_project" in sent:
        user.fireflies_sync_project_id = _project_id(db, user, payload.sync_project)
    db.commit()


def _disconnect_sync(db: Session, user: UserModel) -> DisconnectResponse:
    user.fireflies_api_key_encrypted = None
    user.fireflies_account_email = None
    user.fireflies_sync_project_id = None
    user.fireflies_auto_sync = False
    result = db.execute(
        text(
            "UPDATE meetings SET purge_at = now() + interval '7 days' "
            "WHERE user_id = :u AND source = 'fireflies_sync' AND purge_at IS NULL"
        ),
        {"u": user.id},
    )
    purge_at = db.execute(text("SELECT now() + interval '7 days'")).scalar()  # same transaction, same now()
    db.commit()
    return DisconnectResponse(purge_at=purge_at, meetings_scheduled=result.rowcount)


def _rate_limited(exc: FirefliesAPIError) -> HTTPException:
    headers = None
    if exc.retry_after is not None:
        retry_at = exc.retry_after if exc.retry_after.tzinfo else exc.retry_after.replace(tzinfo=UTC)
        headers = {"Retry-After": str(max(1, int((retry_at - datetime.now(UTC)).total_seconds())))}
    return _error(status.HTTP_429_TOO_MANY_REQUESTS, "Fireflies is rate limiting requests. Try again later.", headers)


@router.post("/connect", response_model=StatusResponse, summary="Link a Fireflies account")
async def connect(
    payload: ConnectRequest,
    user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
    _limit: None = Depends(limit_by_user("rate_limit_fireflies_connect")),
) -> StatusResponse:
    _require_configured()
    await _check_password(user, payload.password.get_secret_value())
    if user.fireflies_api_key_encrypted:
        raise _error(status.HTTP_409_CONFLICT, "A Fireflies account is already linked. Disconnect it first.")
    key = payload.api_key.get_secret_value()
    try:
        info = await FirefliesClient(api_key=key).get_current_user()
    except FirefliesAPIError as exc:
        kind = classify_fireflies_error(exc)
        logger.info(f"Fireflies key validation failed for user {user.id}: {kind}")
        if kind in ("key_rejected", "no_access"):
            raise _error(status.HTTP_400_BAD_REQUEST, "Fireflies rejected this API key.") from exc
        if kind == "rate_limited":
            raise _rate_limited(exc) from exc
        raise _error(status.HTTP_502_BAD_GATEWAY, "Could not reach Fireflies. Try again.") from exc
    await run_in_threadpool(_store_link, db, user, key, info.get("email"), payload.sync_project)
    logger.info(f"Fireflies account linked for user {user.id}")
    await start_sync(user.id)
    return await _status(db, user)


@router.get("/status", response_model=StatusResponse, summary="Fireflies link and sync status")
async def get_status(user: UserModel = Depends(get_current_user), db: Session = Depends(get_db)) -> StatusResponse:
    return await _status(db, user)


@router.patch("/settings", response_model=StatusResponse, summary="Change Fireflies sync settings")
async def update_settings(
    payload: SettingsRequest, user: UserModel = Depends(get_current_user), db: Session = Depends(get_db)
) -> StatusResponse:
    _require_linked(user)
    await run_in_threadpool(_apply_settings, db, user, payload)
    return await _status(db, user)


@router.post("/sync", status_code=status.HTTP_202_ACCEPTED, summary="Start a Fireflies sync")
async def sync(
    payload: SyncRequest | None = None, user: UserModel = Depends(get_current_user)
) -> dict[str, str]:
    _require_configured()
    _require_linked(user)
    kwargs = {"include_deleted": True} if payload and payload.include_deleted else {}
    if not await start_sync(user.id, **kwargs):
        raise _error(status.HTTP_409_CONFLICT, "A sync is already running.")
    return {"status": "started"}


@router.post("/disconnect", response_model=DisconnectResponse, summary="Unlink Fireflies")
async def disconnect(
    payload: DisconnectRequest, user: UserModel = Depends(get_current_user), db: Session = Depends(get_db)
) -> DisconnectResponse:
    await _check_password(user, payload.password.get_secret_value())
    _require_linked(user)
    result = await run_in_threadpool(_disconnect_sync, db, user)
    logger.info(f"Fireflies account unlinked for user {user.id}; {result.meetings_scheduled} meetings scheduled")
    return result
