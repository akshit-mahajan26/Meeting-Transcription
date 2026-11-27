"""Rate limits (plan 23.3): moving-window counters of the `limits` library, kept in this process's memory.

Limits are FastAPI dependencies: `limit_by_user("rate_limit_query")` keys on the signed-in account,
`limit_by_ip("rate_limit_signup_ip")` on the client IP (the browser's IP: the frontend forwards it in
X-Forwarded-For and uvicorn trusts that header only from FORWARDED_ALLOW_IPS). A limit that is exceeded answers
429 with Retry-After. Counters are per backend process and start empty after a restart (no extra service, no
table); with one backend process the limits are exact. Any limiter error fails open (request allowed, ERROR logged).
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Awaitable, Callable

from fastapi import Depends, HTTPException, Request, status
from limits import RateLimitItem, parse_many
from limits.aio.storage import MemoryStorage
from limits.aio.strategies import MovingWindowRateLimiter

from ..api.deps import get_current_user
from .config import get_settings
from .database import UserModel

logger = logging.getLogger(__name__)

_limiter: MovingWindowRateLimiter | None = None


def _get_limiter() -> MovingWindowRateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = MovingWindowRateLimiter(MemoryStorage())
    return _limiter


def _items(setting_name: str) -> list[RateLimitItem]:
    return parse_many(getattr(get_settings(), setting_name))


def client_ip(request: Request) -> str:
    """The caller's IP as uvicorn resolved it (X-Forwarded-For applied only for trusted proxies)."""
    return request.client.host if request.client else "unknown"


def _too_many(retry_after: int, message: str | None = None) -> HTTPException:
    if message is None:
        message = (
            f"Too many requests. Try again in {retry_after} seconds."
            if retry_after < 120
            else f"Too many requests. Try again in {math.ceil(retry_after / 60)} minutes."
        )
    return HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=message, headers={"Retry-After": str(retry_after)})


async def enforce(setting_name: str, identity: str) -> None:
    """Counts one request against every limit in the setting; raises 429 when one is exceeded. Fails open."""
    try:
        for item in _items(setting_name):
            if not await _get_limiter().hit(item, setting_name, identity):
                raise _too_many(await _retry_after_for(item, setting_name, identity))
    except HTTPException:
        raise
    except Exception:
        logger.exception(f"Rate limiter unavailable for {setting_name}; allowing the request")


async def _retry_after_for(item: RateLimitItem, setting_name: str, identity: str) -> int:
    stats = await _get_limiter().get_window_stats(item, setting_name, identity)
    return max(1, math.ceil(stats.reset_time - time.time()))


def limit_by_user(setting_name: str) -> Callable[..., Awaitable[None]]:
    """Dependency: one hit per request for the signed-in account."""

    async def dependency(user: UserModel = Depends(get_current_user)) -> None:
        await enforce(setting_name, f"user:{user.id}")

    return dependency


def limit_by_ip(setting_name: str) -> Callable[..., Awaitable[None]]:
    """Dependency: one hit per request for the client IP."""

    async def dependency(request: Request) -> None:
        await enforce(setting_name, f"ip:{client_ip(request)}")

    return dependency


# Login failures (plan 23.3): counted per normalised email, failed attempts only. Temporary throttling, never a
# permanent lockout, and applied to unknown emails too, so the response never reveals whether an account exists.

_LOGIN_FAILURES = "rate_limit_login_failures"


async def login_throttled(email: str) -> int | None:
    """Seconds to wait when this email has used up its failed attempts, else None. Fails open."""
    try:
        for item in _items(_LOGIN_FAILURES):
            if not await _get_limiter().test(item, _LOGIN_FAILURES, f"email:{email}"):
                return await _retry_after_for(item, _LOGIN_FAILURES, f"email:{email}")
    except Exception:
        logger.exception("Rate limiter unavailable for login failures; allowing the attempt")
    return None


async def record_login_failure(email: str) -> None:
    try:
        for item in _items(_LOGIN_FAILURES):
            await _get_limiter().hit(item, _LOGIN_FAILURES, f"email:{email}")
    except Exception:
        logger.exception("Rate limiter unavailable; login failure not counted")


async def clear_login_failures(email: str) -> None:
    try:
        for item in _items(_LOGIN_FAILURES):
            await _get_limiter().clear(item, _LOGIN_FAILURES, f"email:{email}")
    except Exception:
        logger.exception("Rate limiter unavailable; login failures not cleared")


def login_throttled_error(retry_after: int) -> HTTPException:
    minutes = max(1, math.ceil(retry_after / 60))
    return _too_many(retry_after, f"Too many failed attempts. Try again in {minutes} minutes.")
