"""Password hashing and JWT access tokens."""

import uuid
from datetime import UTC, datetime

import bcrypt
import jwt

from .config import get_settings

MAX_BCRYPT_BYTES = 72


def hash_password(password: str) -> str:
    """Bcrypt hash of the password (stored as text)."""
    return bcrypt.hashpw(password.encode("utf-8")[:MAX_BCRYPT_BYTES], bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8")[:MAX_BCRYPT_BYTES], password_hash.encode("utf-8"))
    except ValueError:
        return False


REFRESH_AFTER_SECONDS = 60


def issue_access_token(user_id: str, auth_time: int | None = None) -> tuple[str, datetime, datetime | None]:
    """Signs a token; returns (token, expires_at, refresh_after).

    auth_time is the login time in epoch seconds (now for a fresh login, kept on refresh).
    exp = auth_time + max, or with the idle limit on min(iat + idle, auth_time + max).
    refresh_after is iat + 60 s when the idle limit is on, else None.
    """
    settings = get_settings()
    iat = int(datetime.now(UTC).timestamp())
    auth_time = iat if auth_time is None else int(auth_time)
    exp = auth_time + settings.access_token_expire_minutes * 60
    idle = settings.session_idle_timeout_minutes
    if idle > 0:
        exp = min(iat + idle * 60, exp)
    claims = {"sub": user_id, "auth_time": auth_time, "exp": exp, "iat": iat, "jti": uuid.uuid4().hex}
    token = jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    refresh_after = datetime.fromtimestamp(iat + REFRESH_AFTER_SECONDS, UTC) if idle > 0 else None
    return token, datetime.fromtimestamp(exp, UTC), refresh_after


def create_access_token(user_id: str, auth_time: int | None = None) -> str:
    return issue_access_token(user_id, auth_time)[0]


def decode_access_token_claims(token: str) -> dict | None:
    """Returns the verified payload of a valid token, or None if it is invalid, expired or has no user id."""
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError:
        return None
    sub = payload.get("sub")
    return payload if isinstance(sub, str) and sub else None


def decode_access_token(token: str) -> str | None:
    """Returns the user id from a valid token, or None if it is invalid or expired."""
    claims = decode_access_token_claims(token)
    return claims["sub"] if claims else None
