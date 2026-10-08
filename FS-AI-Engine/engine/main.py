from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .core.settings import get_settings
from .api.routes import router as transcript_router, chat_router
from .api.routers.engine import router as engine_router

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

# Include versioned API routers
app.include_router(transcript_router, prefix="/api/v1")
app.include_router(transcript_router, prefix="/api/v1/transcript")
app.include_router(chat_router, prefix="/api/v1")
# Register engine router under both /api/v1 and /api/v1/engine
app.include_router(engine_router, prefix="/api/v1/engine")
app.include_router(engine_router, prefix="/api/v1")


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
