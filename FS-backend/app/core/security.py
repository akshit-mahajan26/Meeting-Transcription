"""Password hashing and JWT access tokens."""

import uuid
from datetime import UTC, datetime, timedelta

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


def create_access_token(user_id: str) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    expires = now + timedelta(minutes=settings.access_token_expire_minutes)
    claims = {"sub": user_id, "exp": expires, "iat": int(now.timestamp()), "jti": uuid.uuid4().hex}
    return jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)


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
