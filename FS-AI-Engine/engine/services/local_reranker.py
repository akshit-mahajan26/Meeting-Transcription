"""
FS-AI-Engine/engine/services/local_reranker.py
==============================================
In-process, zero-cost, privacy-safe Cross-Encoder Reranker using FlashRank (ONNX).
100% local CPU execution. Zero external API calls, zero token costs, zero data transmission.
"""

import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger("fs-ai-engine")


class LocalSafeReranker:
    """
    Local Cross-Encoder Reranker using quantized ONNX models on CPU.
    Evaluates joint cross-attention over [Query, Passage] to re-score
    candidates retrieved from Stage 1 (PostgreSQL RRF).
    """

    def __init__(self, model_name: str = "ms-marco-MiniLM-L-12-v2"):
        self.model_name = model_name
        self._ranker = None
        self._initialized = False

    def _get_ranker(self):
        """Lazy loader for FlashRank Ranker singleton with resilient fallback."""
        if self._initialized:
            return self._ranker

        try:
            from flashrank import Ranker
            logger.info(f"[LocalReranker] Initializing local ONNX model: {self.model_name}")
            self._ranker = Ranker(model_name=self.model_name)
            self._initialized = True
            logger.info(f"[LocalReranker] Successfully loaded {self.model_name}")
        except Exception as e:
            logger.warning(
                f"[LocalReranker] Could not initialize FlashRank ({e}). "
                "Falling back to rank-preserving pass-through."
            )
            self._ranker = None
            self._initialized = True

        return self._ranker

    def rerank(
        self,
        query: str,
        candidates: List[Dict[str, Any]],
        top_k: int = 8,
    ) -> List[Dict[str, Any]]:
        """
        Reranks retrieved candidate turns via cross-attention.

        Args:
            query: The user query string.
            candidates: List of candidate dictionaries with keys 'turn_id', 'speaker', 'text', etc.
            top_k: Maximum number of top candidates to return.

        Returns:
            List of reranked candidates up to top_k, enriched with 'rerank_score'.
        """
        if not candidates:
            return []

        if len(candidates) <= 1:
            return candidates[:top_k]

        ranker = self._get_ranker()
        if ranker is None:
            # Fallback: keep existing order
            return candidates[:top_k]

        try:
            from flashrank import RerankRequest

            # Format passages with rich speaker and topic context for cross-attention
            passages = []
            for idx, c in enumerate(candidates):
                spk = c.get("speaker") or c.get("speaker_name") or "Unknown"
                topic = c.get("topic_context") or c.get("topic_title") or c.get("meeting_title") or ""
                ts = c.get("timestamp_str") or ""
                text = c.get("text") or ""

                parts = [f"Speaker: {spk}"]
                if topic:
                    parts.append(f"Topic: {topic}")
                if ts:
                    parts.append(f"Time: {ts}")
                parts.append(f"Content: {text}")

                passages.append({
                    "id": str(c.get("turn_id", idx)),
                    "text": " | ".join(parts),
                    "meta": c,
                })

            rerank_request = RerankRequest(query=query, passages=passages)
            results = ranker.rerank(rerank_request)

            reranked: List[Dict[str, Any]] = []
            for r in results[:top_k]:
                item = r.get("meta", {})
                item["rerank_score"] = round(float(r.get("score", 0.0)), 4)
                reranked.append(item)

            return reranked

        except Exception as e:
            logger.warning(f"[LocalReranker] Error during reranking ({e}). Falling back to original candidate order.")
            return candidates[:top_k]


# Global singleton instance
local_reranker = LocalSafeReranker()
