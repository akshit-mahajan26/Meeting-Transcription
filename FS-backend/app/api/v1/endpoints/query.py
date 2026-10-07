"""
FS-backend/app/api/v1/endpoints/query.py
=========================================
Transparent API Gateway Proxy forwarding client queries to FS-AI-Engine.
Zero direct vector retrieval, database calls, or LLM code.
"""

from ...routers.query import router, QueryRequest, QueryResponse

__all__ = ["router", "QueryRequest", "QueryResponse"]
