"""Request and response models of the Fireflies account-linking endpoints (plan section 8)."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, SecretStr


class ConnectRequest(BaseModel):
    password: SecretStr
    api_key: SecretStr
    sync_project: str | None = None


class DisconnectRequest(BaseModel):
    password: SecretStr


class SettingsRequest(BaseModel):
    """Only the fields present in the body are applied (model_fields_set)."""

    auto_sync: bool | None = None
    sync_project: str | None = None


class SyncRequest(BaseModel):
    include_deleted: bool = False


class PendingDeletion(BaseModel):
    meetings: int
    purge_at: datetime | None


class StatusResponse(BaseModel):
    linked: bool
    account_email: str | None
    auto_sync: bool
    sync_project: str | None
    sync_running: bool
    sync_heartbeat_at: datetime | None
    last_synced_at: datetime | None
    last_sync_result: dict[str, Any] | None
    pending_deletion: PendingDeletion | None
    deleted_meetings: int = 0


class DisconnectResponse(BaseModel):
    purge_at: datetime
    meetings_scheduled: int
