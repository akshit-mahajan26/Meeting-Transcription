from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.v1.router import api_router
from .core.config import get_settings
from .core.database import init_db

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
