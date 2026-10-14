from fastapi import APIRouter, Depends

from ...api.deps import get_current_user
from ...api.routers.meetings import router as link_ingest_router
from .endpoints.auth import router as auth_router
from .endpoints.meetings import router as meetings_router
from .endpoints.query import router as query_router

api_router = APIRouter()
api_router.include_router(auth_router, prefix="/auth", tags=["auth"])

# Everything below requires a valid login; routes also receive the user to scope their data.
protected = [Depends(get_current_user)]
api_router.include_router(meetings_router, prefix="/meetings", tags=["meetings"], dependencies=protected)
api_router.include_router(link_ingest_router, prefix="", tags=["meetings"], dependencies=protected)
api_router.include_router(query_router, prefix="", tags=["query"], dependencies=protected)
