from contextlib import asynccontextmanager
from typing import Dict, Any, Optional
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from .core.config import get_settings
from .core.database import init_db
from .api.v1.router import api_router
from .api.v1.endpoints.meetings import handle_fireflies_webhook_manual
from .schemas.models import FirefliesWebhookPayload, WebhookResponse
from .services.meeting_service import MeetingService
from .core.database import SessionLocal

settings = get_settings()


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
# Root and Fallback Webhook Endpoints
# (Ensures 100% delivery success regardless of whether URL has /api/v1 prefix)
# ------------------------------------------------------------------------------

@app.get("/", tags=["system"])
def root_check():
    return {
        "status": "healthy",
        "service": "FS-backend",
        "version": settings.app_version,
        "webhook_url": "/api/v1/webhook/fireflies",
    }


@app.get("/health", tags=["system"])
def health_check():
    return {
        "status": "healthy",
        "service": "FS-backend",
        "version": settings.app_version,
        "environment": settings.backend_env,
    }


@app.get("/webhook/fireflies", tags=["webhook"])
@app.get("/api/v1/webhook/fireflies", tags=["webhook"])
@app.get("/webhook", tags=["webhook"])
@app.get("/api/v1/webhook", tags=["webhook"])
def webhook_health_ping():
    """Verification ping handler for webhook uptime checks."""
    return {"status": "ok", "message": "Fireflies webhook endpoint active and listening."}


@app.post("/webhook/fireflies", response_model=WebhookResponse, tags=["webhook"])
@app.post("/webhook", response_model=WebhookResponse, tags=["webhook"])
@app.post("/api/v1/webhook", response_model=WebhookResponse, tags=["webhook"])
async def root_webhook_alias(request: Request):
    """Fallback webhook route handling both prefixed and non-prefixed paths."""
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    db = SessionLocal()
    try:
        service = MeetingService(db=db, settings=settings)
        return service.handle_webhook_event(payload)
    finally:
        db.close()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.backend_host,
        port=settings.backend_port,
        reload=(settings.backend_env == "development"),
    )
