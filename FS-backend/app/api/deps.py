"""Shared FastAPI dependencies: the authenticated user for protected routes."""

import logging
from datetime import UTC

from fastapi import Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from ..core.database import UserModel, get_db
from ..core.security import decode_access_token_claims

logger = logging.getLogger(__name__)

bearer_scheme = HTTPBearer(auto_error=False)


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated. Please log in again.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _revoked(claims: dict, user: UserModel) -> bool:
    """True when the token was issued at or before the user's sign-out cutoff."""
    cutoff = user.tokens_valid_after
    if cutoff is None:
        return False
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=UTC)
    iat = claims.get("iat")
    return (iat if isinstance(iat, int | float) else 0) <= int(cutoff.timestamp())


async def get_current_claims(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> dict:
    """Verified claims of the presented bearer token, or 401 if it is missing, invalid, or expired."""
    if credentials is None:
        raise _unauthorized()
    claims = decode_access_token_claims(credentials.credentials)
    if claims is None:
        raise _unauthorized()
    return claims


async def get_current_user(
    claims: dict = Depends(get_current_claims),
    db: Session = Depends(get_db),
) -> UserModel:
    """Resolves the bearer token to an existing user, or answers 401."""
    user = await run_in_threadpool(lambda: db.query(UserModel).filter(UserModel.id == claims["sub"]).first())
    if user is None or _revoked(claims, user):
        raise _unauthorized()
    return user
