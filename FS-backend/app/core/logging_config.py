"""Central logging: one setup call, a request-ID context, JSON output for production, and an access-log middleware.

Env vars:
  LOG_LEVEL   DEBUG | INFO | WARNING | ERROR   (default INFO)
  LOG_FORMAT  json | text                       (default json when BACKEND_ENV=production, else text)

Every request gets an ID (taken from the incoming X-Request-ID header or generated). It is attached to every log
line emitted while handling that request, returned in the response header, and forwarded to downstream services, so
one ID can be searched across the frontend, backend and AI engine logs.
"""
import contextvars
import json
import logging
import os
import sys
import time
import uuid

REQUEST_ID_HEADER = "X-Request-ID"
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

_RESERVED = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}
_QUIET_PATHS = {"/health", "/api/v1/health", "/"}  # probes hit these every few seconds; log at DEBUG only


def get_request_id() -> str:
    return request_id_var.get()


def outbound_headers() -> dict[str, str]:
    """Headers to add to calls to other services so the request ID follows the call."""
    rid = request_id_var.get()
    return {REQUEST_ID_HEADER: rid} if rid != "-" else {}


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class _JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S") + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "message": record.getMessage(),
        }
        entry.update({k: v for k, v in record.__dict__.items() if k not in _RESERVED and k != "request_id"})
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


class _TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        extras = {k: v for k, v in record.__dict__.items() if k not in _RESERVED and k != "request_id"}
        suffix = " " + " ".join(f"{k}={v}" for k, v in extras.items()) if extras else ""
        base = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} [{getattr(record, 'request_id', '-')[:8]}] {record.name}: {record.getMessage()}{suffix}"
        return base + ("\n" + self.formatException(record.exc_info) if record.exc_info else "")


def setup_logging(service: str, env: str = "development") -> None:
    """Configure the root logger and route uvicorn's loggers through it. Safe to call more than once."""
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    fmt = os.getenv("LOG_FORMAT", "json" if env == "production" else "text").lower()

    handler = logging.StreamHandler(sys.stdout)  # containers collect stdout; the platform ships it to the log store
    handler.addFilter(_RequestIdFilter())
    handler.setFormatter(_JsonFormatter(service) if fmt == "json" else _TextFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers, lg.propagate = [], True
    # Our middleware writes the access log (with request ID, route and timing), so silence uvicorn's duplicate one.
    access = logging.getLogger("uvicorn.access")
    access.handlers, access.propagate, access.disabled = [], False, True
    # Client libraries log full URLs at INFO; keep them to warnings and let our own lines tell the story.
    for noisy in ("httpx", "httpcore", "watchfiles"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class RequestLoggingMiddleware:
    """Pure ASGI middleware: assigns the request ID and writes one structured access-log line per request."""

    def __init__(self, app):
        self.app = app
        self.log = logging.getLogger("http.access")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        rid = headers.get(REQUEST_ID_HEADER.lower()) or uuid.uuid4().hex
        token = request_id_var.set(rid[:64])
        start = time.perf_counter()
        state = {"status": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
                message.setdefault("headers", []).append((REQUEST_ID_HEADER.lower().encode(), rid[:64].encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            self.log.exception("unhandled error", extra=self._fields(scope, headers, state, start))
            raise
        finally:
            status = state["status"]
            path = scope["path"]
            level = logging.DEBUG if path in _QUIET_PATHS and status < 400 else (
                logging.ERROR if status >= 500 else logging.WARNING if status >= 400 else logging.INFO
            )
            self.log.log(level, f"{scope['method']} {path} -> {status}", extra=self._fields(scope, headers, state, start))
            request_id_var.reset(token)

    @staticmethod
    def _fields(scope, headers, state, start) -> dict:
        route = scope.get("route")
        client = scope.get("client")
        return {
            "method": scope["method"],
            "path": scope["path"],  # query strings are deliberately not logged: they can carry tokens / PII
            "route": getattr(route, "path", scope["path"]),
            "status": state["status"],
            "duration_ms": round((time.perf_counter() - start) * 1000, 1),
            "client_ip": (headers.get("x-forwarded-for", "").split(",")[0].strip() or (client[0] if client else "-")),
            "user_agent": headers.get("user-agent", "-")[:120],
        }
