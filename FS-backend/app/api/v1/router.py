from fastapi import APIRouter
from .endpoints.meetings import router as meetings_router, handle_fireflies_webhook_manual
from .endpoints.query import router as query_router
from ...api.routers.meetings import router as link_ingest_router
from ...schemas.models import WebhookResponse

api_router = APIRouter()
api_router.include_router(meetings_router, prefix="/meetings", tags=["meetings"])
api_router.include_router(link_ingest_router, prefix="", tags=["meetings"])
api_router.include_router(query_router, prefix="", tags=["query"])

# Root-level alias: POST /api/v1/webhook/fireflies
api_router.add_api_route(
    "/webhook/fireflies",
    handle_fireflies_webhook_manual,
    methods=["POST"],
    response_model=WebhookResponse,
    summary="Fireflies Webhook & Manual Swagger Ingestion",
    tags=["webhook"]
)
