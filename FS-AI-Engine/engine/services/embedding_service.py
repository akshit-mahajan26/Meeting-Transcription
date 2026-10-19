import asyncio
import hashlib
import logging
import math

from openai import AsyncOpenAI, OpenAIError

from ..core.config import Settings, settings

logger = logging.getLogger(__name__)


class EmbeddingServiceError(Exception):
    """Custom exception raised when embedding generation fails."""


class EmbeddingService:
    """
    Dynamic Embedding Service routed through Google Gemini universal provider.
    Zero Hardcoding: Reads API key, base URL, model name, and vector dimensions dynamically
    from the centralized settings module.
    """

    def __init__(self, config: Settings | None = None):
        self.config = config or settings
        self.api_key = self.config.GEMINI_API_KEY
        self.base_url = self.config.GEMINI_BASE_URL

        # OpenAI model names are not served by Gemini; fall back to the Gemini embedding model
        configured_model = self.config.embedding_model or "gemini-embedding-001"
        if "text-embedding" in configured_model or "openai" in configured_model:
            self.model = "gemini-embedding-001"
        else:
            self.model = configured_model

        self.dimensions = self.config.embedding_dimensions or 1536

        # Initialize AsyncOpenAI client targeting Gemini endpoint
        self.client: AsyncOpenAI | None = None
        if self.api_key and self.api_key.strip():
            self.client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=45.0,
            )

    async def generate_embeddings(self, texts: list[str]) -> list[list[float]]:
        """
        Asynchronously generate high-dimensional vector embeddings for a list of text strings routed through Gemini.

        Args:
            texts: List of text inputs to embed.

        Returns:
            List of float vectors, each having length equal to settings.embedding_dimensions.
        """
        if not texts:
            return []

        # Sanitize texts (replace empty/whitespace strings with a placeholder)
        sanitized_texts = [t.strip() if t.strip() else " " for t in texts]

        # If live client is configured, call embeddings API in batches
        if self.client:
            try:
                batch_size = 50
                batches = [
                    sanitized_texts[i : i + batch_size]
                    for i in range(0, len(sanitized_texts), batch_size)
                ]

                emb_sem = asyncio.Semaphore(4)

                async def _fetch_batch(batch_slice: list[str]) -> list[list[float]]:
                    async with emb_sem:
                        kwargs = {
                            "input": batch_slice,
                            "model": self.model,
                            "dimensions": self.dimensions,
                        }
                        try:
                            resp = await self.client.embeddings.create(**kwargs)
                        except Exception:
                            # Retry without dimensions parameter if rejected by certain endpoints
                            kwargs.pop("dimensions", None)
                            resp = await self.client.embeddings.create(**kwargs)
                        return [item.embedding for item in resp.data]

                batch_results = await asyncio.gather(*[_fetch_batch(b) for b in batches])
                all_embeddings: list[list[float]] = []
                for br in batch_results:
                    all_embeddings.extend(br)

                # Check or pad/truncate dimensions to match database schema
                for idx, emb in enumerate(all_embeddings):
                    if len(emb) != self.dimensions:
                        logger.warning(
                            f"Embedding at index {idx} dimension {len(emb)} differs from expected {self.dimensions}. Normalizing."
                        )
                        # Pad or truncate if necessary to match DB vector column
                        if len(emb) > self.dimensions:
                            all_embeddings[idx] = emb[:self.dimensions]
                        else:
                            all_embeddings[idx] = emb + [0.0] * (self.dimensions - len(emb))

                return all_embeddings
            except (OpenAIError, Exception) as exc:
                # Never substitute fake vectors for a real provider failure: they would be stored as genuine embeddings
                logger.error(f"Gemini embedding generation failed: {exc}")
                raise EmbeddingServiceError(f"Embedding provider failed: {exc}") from exc

        # Deterministic vector generation when API key is not supplied (test / offline mode)
        logger.debug(
            f"Embedding API key not configured. Generating deterministic fallback embeddings "
            f"(dim={self.dimensions}) for {len(texts)} texts."
        )
        return self._generate_deterministic_embeddings(sanitized_texts)

    def _generate_deterministic_embeddings(self, texts: list[str]) -> list[list[float]]:
        """
        Generate deterministic, normalized pseudo-vectors of dimension settings.embedding_dimensions.
        Used for reliable offline testing, CI environments, and graceful degradation.
        """
        embeddings: list[list[float]] = []
        for text in texts:
            # Seed deterministic PRNG from text SHA256
            hasher = hashlib.sha256(text.encode("utf-8"))
            seed_bytes = hasher.digest()

            vector: list[float] = []
            for i in range(self.dimensions):
                # Cycle through seed bytes to generate pseudo-random floats
                b = seed_bytes[i % len(seed_bytes)]
                val = ((b + (i * 31)) % 1000) / 500.0 - 1.0  # value in [-1.0, 1.0]
                vector.append(val)

            # Normalize vector to unit length
            norm = math.sqrt(sum(v * v for v in vector))
            if norm > 0:
                vector = [round(v / norm, 6) for v in vector]
            embeddings.append(vector)

        return embeddings
