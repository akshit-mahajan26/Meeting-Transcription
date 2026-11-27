import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from ..core.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Project root (/Meeting-Transcription); relative raw_transcripts_dir resolves from here
ROOT_DIR = Path(__file__).resolve().parents[3]


TRANSCRIPT_QUERY = """
query GetTranscript($transcriptId: String!) {
  transcript(id: $transcriptId) {
    id
    title
    date
    duration
    organizer_email
    summary {
      keywords
      action_items
      outline
      shorthand_bullet
      overview
      bullet_gist
      gist
    }
    speakers {
      id
      name
    }
    sentences {
      index
      speaker_name
      speaker_id
      text
      raw_text
      start_time
      end_time
    }
  }
}
"""

UPLOAD_AUDIO_MUTATION = """
mutation UploadAudio($input: AudioUploadInput!) {
  uploadAudio(input: $input) {
    success
    title
    message
  }
}
"""


class FirefliesAPIError(Exception):
    """Exception raised when Fireflies API call or GraphQL operation fails."""
    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        details: Any | None = None,
        *,
        code: str | None = None,
        friendly: bool = False,
        retry_after: datetime | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.friendly = friendly
        self.retry_after = retry_after
        self.message = message
        self.status_code = status_code
        self.details = details

    def __str__(self) -> str:
        code_str = f" [HTTP {self.status_code}]" if self.status_code else ""
        return f"FirefliesAPIError{code_str}: {self.message}"


def classify_fireflies_error(exc: FirefliesAPIError) -> str:
    """Map a Fireflies error to one class: not_found, no_access, key_rejected, account_problem,
    rate_limited, client_bug or temporary. Decided by code first, then by status."""
    code = (exc.code or "").lower()
    status = exc.status_code
    if code == "object_not_found":
        return "not_found"
    if code in ("forbidden", "not_in_team", "require_elevated_privilege"):
        return "no_access"
    if code in ("paid_required", "account_cancelled", "feature_disabled", "require_ai_credits"):
        return "account_problem"
    if code == "too_many_requests":
        return "rate_limited"
    if code in ("invalid_arguments", "args_required"):
        return "client_bug"
    if code in ("request_timeout", "invariant_violation"):
        return "temporary"
    if status == 404:
        return "not_found"
    if status == 401:
        return "key_rejected"
    if status == 403:
        return "no_access"
    if status == 402:
        return "account_problem"
    if status == 429:
        return "rate_limited"
    if status == 400:
        return "client_bug"
    return "temporary"


def _parse_error_response(
    response: httpx.Response, body: Any
) -> tuple[str, int, str | None, bool, datetime | None, Any] | None:
    """Read the first GraphQL error of a response body. Returns None when the body has no errors."""
    errors = body.get("errors") if isinstance(body, dict) else None
    if not errors:
        return None
    first = errors[0] if isinstance(errors[0], dict) else {}
    ext = first.get("extensions") if isinstance(first.get("extensions"), dict) else {}
    code = ext.get("code") or first.get("code")
    status = ext.get("status")
    if not isinstance(status, int):
        status = response.status_code if response.status_code != 200 else 400
    retry_after = None
    header = response.headers.get("Retry-After")
    try:
        if header is not None:
            retry_after = datetime.now(UTC) + timedelta(seconds=float(header))
        else:
            meta = ext.get("metadata") if isinstance(ext.get("metadata"), dict) else {}
            if meta.get("retryAfter") is not None:
                retry_after = datetime.fromtimestamp(float(meta["retryAfter"]) / 1000.0, tz=UTC)
    except (ValueError, TypeError, OverflowError, OSError):
        retry_after = None
    message = "; ".join(e.get("message", "Unknown error") if isinstance(e, dict) else str(e) for e in errors)
    return message, status, code, bool(first.get("friendly")), retry_after, errors


def save_transcript_to_file(transcript_data: dict[str, Any], meeting_id: str, user_id: str) -> Path | None:
    """
    Save raw Fireflies transcript dictionary as JSON at <raw_transcripts_dir>/<user_id>/<meeting_id>.json.
    """
    if not transcript_data:
        return None
    try:
        def _safe(value: str) -> str:
            return "".join(c if c.isalnum() or c in ("-", "_", ".") else "_" for c in str(value))

        base = Path(get_settings().raw_transcripts_dir)
        if not base.is_absolute():
            base = ROOT_DIR / base
        target_dir = base / _safe(user_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        file_path = target_dir / f"{_safe(meeting_id)}.json"
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(transcript_data, f, indent=2, ensure_ascii=False)
        logger.info(f"Saved raw Fireflies transcript JSON to {file_path}")
        return file_path
    except Exception as exc:
        logger.error(f"Failed to save transcript JSON for '{meeting_id}': {exc}", exc_info=True)
        return None


class FirefliesClient:
    """
    Dedicated Asynchronous Fireflies.ai GraphQL Client for FS-backend.
    Handles:
    1. Querying transcripts by transcript ID.
    2. Submitting raw media links via the uploadAudio GraphQL mutation.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        api_key: str | None = None,
        api_url: str | None = None,
        timeout: float = 45.0,
    ):
        self.settings = settings or get_settings()
        self.api_key = api_key or self.settings.fireflies_api_key
        self.api_url = api_url or self.settings.fireflies_api_url
        self.timeout = timeout

    async def _execute_graphql(
        self,
        query: str,
        variables: dict[str, Any],
        max_retries: int = 3,
        initial_backoff: float = 1.0,
    ) -> dict[str, Any]:
        """Executes a GraphQL query or mutation with exponential backoff retries."""
        if not self.api_key or not self.api_key.strip():
            raise FirefliesAPIError("FIREFLIES_API_KEY is not configured in backend.", status_code=401)

        headers = {
            "Authorization": f"Bearer {self.api_key.strip()}",
            "Content-Type": "application/json",
        }
        payload = {"query": query, "variables": variables}

        attempt = 0
        backoff = initial_backoff
        while attempt < max_retries:
            attempt += 1
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.post(self.api_url, json=payload, headers=headers)

                if response.status_code >= 500:
                    logger.warning(
                        f"Fireflies server returned HTTP {response.status_code} on attempt {attempt}/{max_retries}. "
                        f"Retrying in {backoff}s..."
                    )
                    await asyncio.sleep(backoff)
                    backoff *= 2.0
                    continue

                if response.status_code != 200:
                    try:
                        parsed = _parse_error_response(response, response.json())
                    except ValueError:
                        parsed = None
                    if parsed:
                        err_msg, status, code, friendly, retry_after, errors = parsed
                        raise FirefliesAPIError(
                            f"Fireflies returned HTTP {response.status_code}: {err_msg}",
                            status_code=status,
                            details=errors,
                            code=code,
                            friendly=friendly,
                            retry_after=retry_after,
                        )
                    header = response.headers.get("Retry-After")
                    retry_after = None
                    try:
                        if header is not None:
                            retry_after = datetime.now(UTC) + timedelta(seconds=float(header))
                    except ValueError:
                        pass
                    raise FirefliesAPIError(
                        f"Fireflies returned HTTP {response.status_code}: {response.text}",
                        status_code=response.status_code,
                        retry_after=retry_after,
                    )

                data = response.json()
                parsed = _parse_error_response(response, data)
                if parsed:
                    err_msg, status, code, friendly, retry_after, errors = parsed
                    raise FirefliesAPIError(
                        f"GraphQL Error from Fireflies: {err_msg}",
                        status_code=status,
                        details=errors,
                        code=code,
                        friendly=friendly,
                        retry_after=retry_after,
                    )

                return data.get("data", {})

            except (httpx.TimeoutException, httpx.RequestError) as exc:
                if attempt >= max_retries:
                    logger.error(f"Fireflies API network failure after {max_retries} attempts: {exc}")
                    raise FirefliesAPIError(
                        f"Connection to Fireflies API failed after {max_retries} retries: {exc!s}",
                        status_code=504 if isinstance(exc, httpx.TimeoutException) else 503,
                    ) from exc
                logger.warning(f"Network error on attempt {attempt}/{max_retries}: {exc}. Retrying in {backoff}s...")
                await asyncio.sleep(backoff)
                backoff *= 2.0

        raise FirefliesAPIError("Max retries exceeded communicating with Fireflies.", status_code=500)

    async def fetch_transcript_by_id(
        self, transcript_id: str, save_to_file: bool = False, user_id: str | None = None
    ) -> dict[str, Any]:
        """
        Query Fireflies GraphQL endpoint for a complete meeting transcript.

        Args:
            transcript_id: The unique Fireflies transcript ID.

        Returns:
            dict containing meeting title, date, duration, participants/speakers, and sentences.
        """
        logger.info(f"Fetching transcript for ID: '{transcript_id}' from Fireflies...")
        data = await self._execute_graphql(TRANSCRIPT_QUERY, {"transcriptId": transcript_id})
        transcript_data = data.get("transcript")
        if not transcript_data:
            raise FirefliesAPIError(
                f"Transcript with ID '{transcript_id}' not found in Fireflies.",
                status_code=404,
                code="object_not_found",
            )

        if save_to_file and user_id:
            save_transcript_to_file(transcript_data, transcript_id, user_id)

        sentences = transcript_data.get("sentences", []) or []
        speakers = transcript_data.get("speakers", []) or []

        # Derive speakers if missing
        if not speakers and sentences:
            seen = set()
            for s in sentences:
                name = s.get("speaker_name") or "Unknown Speaker"
                sid = s.get("speaker_id") or name
                if sid not in seen:
                    seen.add(sid)
                    speakers.append({"id": sid, "name": name})

        return {
            "id": str(transcript_data.get("id") or transcript_id),
            "title": str(transcript_data.get("title") or f"Meeting {transcript_id}"),
            "date": str(transcript_data.get("date")) if transcript_data.get("date") else None,
            "duration": float(transcript_data.get("duration") or 0.0),
            "summary": transcript_data.get("summary"),
            "speakers": speakers,
            "sentences": sentences,
            "raw_payload": transcript_data,
        }

    async def upload_audio_from_url(self, audio_url: str, title: str | None = None) -> dict[str, Any]:
        """
        Submit an external raw media recording URL (mp3, wav, mp4, m4a) to Fireflies for async transcription.

        Args:
            audio_url: Public direct link to audio/video file.
            title: Optional custom meeting title.

        Returns:
            dict with uploadAudio mutation confirmation.
        """
        logger.info(f"Submitting audio URL to Fireflies uploadAudio mutation: '{audio_url}'...")
        input_vars = {
            "input": {
                "url": audio_url,
                "title": title or "External Meeting Recording",
            }
        }
        data = await self._execute_graphql(UPLOAD_AUDIO_MUTATION, input_vars)
        upload_res = data.get("uploadAudio") or {}
        logger.info(f"Audio upload mutation result: {upload_res}")
        return upload_res


fireflies_client = FirefliesClient()


def get_fireflies_client() -> FirefliesClient:
    return fireflies_client
