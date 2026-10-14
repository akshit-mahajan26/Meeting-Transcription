from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.routers.engine import router as engine_router
from .api.routes import router as transcript_router
from .core.config import get_settings
from .core.security import verify_internal_key

settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Meeting Transcript Intelligence Platform - Core AI & Processing Engine",
    docs_url="/docs",
    openapi_url="/openapi.json",
)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Versioned API routers (each mounted once):
#   /api/v1/transcript/*  - ingestion pipeline steps (used by FS-backend)
#   /api/v1/engine/query  - question answering (used by FS-backend)
# Both require the shared internal API key (sent by FS-backend after it has verified the user's login).
app.include_router(transcript_router, prefix="/api/v1", dependencies=[Depends(verify_internal_key)])
app.include_router(engine_router, prefix="/api/v1/engine", dependencies=[Depends(verify_internal_key)])


@app.get("/health", tags=["system"])
def health_check():
    """Health check endpoint for AI Engine."""
    return {
        "status": "healthy",
        "service": "FS-AI-Engine",
        "version": settings.app_version,
        "environment": settings.ai_engine_env,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "engine.main:app",
        host=settings.ai_engine_host,
        port=settings.ai_engine_port,
        reload=(settings.ai_engine_env == "development"),
    )
