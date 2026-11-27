import asyncio
import logging
import os
from collections.abc import AsyncIterator

from openai import AsyncOpenAI

from .llm_fallback import heuristic_fallback

try:
    from ..core.config import settings
except (ImportError, ValueError):
    try:
        from core.config import settings
    except (ImportError, ValueError):
        from engine.core.config import settings

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except ImportError:
    pass

logger = logging.getLogger(__name__)

_semaphores: dict[str, asyncio.Semaphore] = {}


def _limit(purpose: str) -> asyncio.Semaphore:
    """Process-wide concurrency limit for LLM calls, created lazily: 'chat' or 'ingest'."""
    if purpose not in ("chat", "ingest"):
        raise ValueError(f"purpose must be 'chat' or 'ingest', got {purpose!r}")
    sem = _semaphores.get(purpose)
    if sem is None:
        size = settings.llm_chat_max_concurrency if purpose == "chat" else settings.llm_ingest_max_concurrency
        sem = _semaphores[purpose] = asyncio.Semaphore(size)
    return sem


class LLMClient:
    """
    Centralized Asynchronous LLM Client Utility.
    Serves as the universal LLM provider interface supporting Google Gemini (via official OpenAI-compatible endpoint and google-genai SDK).
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        default_model: str | None = None,
    ):
        self._client: AsyncOpenAI | None = None
        self._genai_client = None
        self.reload_config(api_key, base_url, default_model)

    def reload_config(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        default_model: str | None = None,
    ):
        """Dynamically re-evaluates environment variables and .env configuration."""
        try:
            from dotenv import load_dotenv
            load_dotenv(override=True)
            from pathlib import Path
            engine_root = Path(__file__).resolve().parent.parent
            load_dotenv(engine_root / ".env", override=True)
        except Exception:
            pass

        # Resolve Gemini API configuration directly
        self.gemini_api_key = (
            api_key
            or os.getenv("GEMINI_API_KEY")
            or getattr(settings, "GEMINI_API_KEY", None)
            or os.getenv("GOOGLE_API_KEY")
        )
        self.api_key = self.gemini_api_key
        self.base_url = (
            base_url
            or getattr(settings, "GEMINI_BASE_URL", None)
            or os.getenv("GEMINI_BASE_URL")
            or "https://generativelanguage.googleapis.com/v1beta/openai/"
        )
        self.default_model = (
            default_model
            or os.getenv("LLM_MODEL_NAME")
            or getattr(settings, "LLM_MODEL_NAME", "gemini-2.5-flash")
        )

        if self.gemini_api_key and not self.gemini_api_key.startswith("mock"):
            try:
                from google import genai
                self._genai_client = genai.Client(api_key=self.gemini_api_key)
            except Exception as e:
                logger.debug(f"google-genai client init note: {e}")

        if self.api_key and not self.api_key.startswith("mock"):
            try:
                self._client = AsyncOpenAI(
                    base_url=self.base_url,
                    api_key=self.api_key,
                    max_retries=settings.llm_max_retries,
                    timeout=settings.llm_timeout_seconds,
                )
                logger.info(f"[LLMClient] Initialized Gemini client (Endpoint: {self.base_url}, Default Model: {self.default_model})")
            except Exception as e:
                logger.warning(f"Could not initialize AsyncOpenAI with Gemini: {e}")
                self._client = None

    @property
    def client(self) -> AsyncOpenAI | None:
        return self._client

    async def generate_chat_completion(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        max_tokens: int = 800,
        temperature: float = 0.2,
        purpose: str = "chat",
    ) -> str:
        """Chat completion, limited to the concurrency of its purpose ('chat' or 'ingest')."""
        async with _limit(purpose):
            return await self._generate_chat_completion(messages, model, max_tokens, temperature)

    async def stream_chat_completion(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        max_tokens: int = 800,
        temperature: float = 0.2,
        purpose: str = "chat",
    ) -> AsyncIterator[str]:
        """Streaming twin of generate_chat_completion; the slot is held for the whole stream and always released."""
        async with _limit(purpose):
            inner = self._stream_chat_completion(messages, model, max_tokens, temperature)
            try:
                async for delta in inner:
                    yield delta
            finally:
                await inner.aclose()

    async def _generate_chat_completion(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        max_tokens: int = 800,
        temperature: float = 0.2,
    ) -> str:
        """
        Sends asynchronous chat completion to Google Gemini.
        Provides robust conversational fallback responses if offline or API key is not configured.
        """
        target_model = self._normalize_model(model)

        # Ensure sufficient token budget for reasoning models like Gemini 2.5 Flash whose internal thinking tokens consume max_tokens
        effective_max_tokens = max(max_tokens or 8192, 8192)

        # 1. Try OpenAI-compatible endpoint (Google Gemini OpenAI endpoint)
        if self._client is not None:
            try:
                logger.info(f"[LLMClient] Sending request to {target_model} via {self.base_url} (max_tokens={effective_max_tokens})")
                response = await self._client.chat.completions.create(
                    model=target_model,
                    messages=messages,
                    max_tokens=effective_max_tokens,
                    temperature=temperature,
                )
                if response.choices and len(response.choices) > 0:
                    content = response.choices[0].message.content or ""
                    return content.strip()
            except Exception as e:
                logger.warning(f"[LLMClient] Gemini API call failed: {e}. Trying secondary path.")

        # 2. Try Google GenAI SDK if available
        if self._genai_client is not None:
            try:
                logger.info(f"[LLMClient] Sending request to {target_model} via Google GenAI SDK")
                # Format system message + user messages for Google GenAI
                sys_parts = [m["content"] for m in messages if m.get("role") == "system"]
                sys_instruction = "\n".join(sys_parts) if sys_parts else None
                # Keep conversation roles (the GenAI SDK calls the assistant "model")
                user_content = [
                    {"role": "model" if m.get("role") == "assistant" else "user", "parts": [{"text": m["content"]}]}
                    for m in messages
                    if m.get("role") != "system"
                ]

                config = {}
                if sys_instruction:
                    config["system_instruction"] = sys_instruction
                if temperature is not None:
                    config["temperature"] = temperature
                if max_tokens:
                    config["max_output_tokens"] = max_tokens

                # Use async client if loop is active
                res = await self._genai_client.aio.models.generate_content(
                    model=target_model,
                    contents=user_content,
                    config=config if config else None,
                )
                if res and res.text:
                    return res.text.strip()
            except Exception as e:
                logger.warning(f"[LLMClient] Google GenAI SDK call failed: {e}. Executing conversational synthesis fallback.")

        # Deterministic Conversational Fallback
        return heuristic_fallback(messages)

    def _normalize_model(self, model: str | None) -> str:
        """The model name the Gemini endpoint accepts (provider prefixes stripped)."""
        target_model = model or self.default_model
        if target_model.startswith("google/"):
            target_model = target_model[len("google/"):]
        elif target_model.startswith("gemini/"):
            target_model = target_model[len("gemini/"):]
        return target_model

    async def _stream_chat_completion(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        max_tokens: int = 800,
        temperature: float = 0.2,
    ) -> AsyncIterator[str]:
        """
        Streams the completion as text deltas. Provider order matches generate_chat_completion: the OpenAI-compatible
        endpoint, then the Google GenAI SDK, then the offline fallback (one chunk). A provider that fails before it
        produced any text is skipped; one that fails after producing text raises, because switching provider mid-answer
        would splice two different answers together (the caller decides how to recover).
        """
        target_model = self._normalize_model(model)
        effective_max_tokens = max(max_tokens or 8192, 8192)
        started = False

        if self._client is not None:
            try:
                logger.info(f"[LLMClient] Streaming request to {target_model} via {self.base_url} (max_tokens={effective_max_tokens})")
                stream = await self._client.chat.completions.create(
                    model=target_model,
                    messages=messages,
                    max_tokens=effective_max_tokens,
                    temperature=temperature,
                    stream=True,
                )
                async for chunk in stream:
                    delta = chunk.choices[0].delta.content if chunk.choices else None
                    if delta:
                        started = True
                        yield delta
                if started:
                    return
            except Exception as e:
                if started:
                    raise
                logger.warning(f"[LLMClient] Gemini streaming call failed: {e}. Trying secondary path.")

        if self._genai_client is not None:
            try:
                logger.info(f"[LLMClient] Streaming request to {target_model} via Google GenAI SDK")
                config = {"temperature": temperature} if temperature is not None else {}
                sys_parts = [m["content"] for m in messages if m.get("role") == "system"]
                if sys_parts:
                    config["system_instruction"] = "\n".join(sys_parts)
                if max_tokens:
                    config["max_output_tokens"] = max_tokens
                contents = [
                    {"role": "model" if m.get("role") == "assistant" else "user", "parts": [{"text": m["content"]}]}
                    for m in messages
                    if m.get("role") != "system"
                ]
                stream = await self._genai_client.aio.models.generate_content_stream(
                    model=target_model, contents=contents, config=config or None
                )
                async for chunk in stream:
                    if chunk.text:
                        started = True
                        yield chunk.text
                if started:
                    return
            except Exception as e:
                if started:
                    raise
                logger.warning(f"[LLMClient] Google GenAI streaming call failed: {e}. Executing conversational synthesis fallback.")

        yield heuristic_fallback(messages)


# Singleton client instance
llm_client_instance = LLMClient()


def get_llm_client() -> LLMClient:
    """Dependency injector for LLMClient."""
    global llm_client_instance
    if llm_client_instance is None:
        llm_client_instance = LLMClient()
    else:
        # Re-initialize if a Gemini key became available after startup
        active_gemini_key = os.getenv("GEMINI_API_KEY") or getattr(settings, "GEMINI_API_KEY", None)
        if active_gemini_key and llm_client_instance._client is None:
            llm_client_instance.reload_config()
    return llm_client_instance
