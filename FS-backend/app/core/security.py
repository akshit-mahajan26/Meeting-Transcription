"""Password hashing and JWT access tokens."""

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
    expires = datetime.now(UTC) + timedelta(minutes=settings.access_token_expire_minutes)
    return jwt.encode({"sub": user_id, "exp": expires}, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> str | None:
    """Returns the user id from a valid token, or None if it is invalid or expired."""
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError:
        return None
    sub = payload.get("sub")
    return sub if isinstance(sub, str) and sub else None
