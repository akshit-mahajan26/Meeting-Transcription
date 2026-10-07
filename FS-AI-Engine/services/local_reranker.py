"""
FS-AI-Engine/services/local_reranker.py
=======================================
Shim exporting LocalSafeReranker from engine.services.local_reranker.
"""

from engine.services.local_reranker import LocalSafeReranker, local_reranker

__all__ = ["LocalSafeReranker", "local_reranker"]
