from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api.v1.router import api_router
from .core.config import get_settings
from .core.database import init_db

settings = get_settings()


class MaxBodySizeMiddleware:
    """Pure ASGI guard: 413 for request bodies over the limit, without buffering or touching responses."""

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = False
        rejected = False

        async def reject():
            nonlocal rejected
            rejected = True
            await JSONResponse({"detail": "Request body too large."}, status_code=413)(scope, receive, send)

        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    too_big = int(value) > self.max_bytes
                except ValueError:
                    too_big = False
                if too_big:
                    await reject()
                    return
                break

        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes and not started and not rejected:
                    await reject()
                    return {"type": "http.disconnect"}
            return message

        async def tracking_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            if rejected:
                return
            await send(message)

        await self.app(scope, limited_receive, tracking_send)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan event handler to initialize database schema on startup."""
    init_db()
    yield


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="API Gateway & Ingestion Router for Meeting Transcript Intelligence Platform",
    docs_url="/docs",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(MaxBodySizeMiddleware, max_bytes=settings.max_request_body_bytes)

# CORS Middleware
origins = settings.cors_origins
if isinstance(origins, str):
    origins = [origins]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins if origins != ["*"] else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register versioned API router under /api/v1
app.include_router(api_router, prefix=settings.api_v1_str)


# ------------------------------------------------------------------------------
# Root & health endpoints
# ------------------------------------------------------------------------------

@app.get("/", tags=["system"])
def root_check():
    return {
        "status": "healthy",
        "service": "FS-backend",
        "version": settings.app_version,
    }


@app.get("/health", tags=["system"])
@app.get("/api/v1/health", tags=["system"])
def health_check():
    return {
        "status": "healthy",
        "service": "FS-backend",
        "version": settings.app_version,
        "environment": settings.backend_env,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.backend_host,
        port=settings.backend_port,
        reload=(settings.backend_env == "development"),
    )
