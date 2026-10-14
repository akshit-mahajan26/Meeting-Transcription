"""Internal authentication: the engine only answers calls that carry the shared API key."""

import hmac

from fastapi import Header, HTTPException, status

from .config import get_settings


def verify_internal_key(x_internal_api_key: str | None = Header(default=None)) -> None:
    """Rejects any request that does not present the key shared with FS-backend."""
    expected = get_settings().engine_api_key
    if not x_internal_api_key or not hmac.compare_digest(x_internal_api_key.encode(), expected.encode()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing internal API key.")
