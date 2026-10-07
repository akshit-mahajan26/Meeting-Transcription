import asyncio
import logging
from typing import Optional, Dict, Any, List
import httpx
from ..core.config import Settings, get_settings

logger = logging.getLogger(__name__)

TRANSCRIPT_QUERY = """
query GetTranscript($transcriptId: String!) {
  transcript(id: $transcriptId) {
    id
    title
    date
    duration
    organizer_email
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
    def __init__(self, message: str, status_code: Optional[int] = None, details: Optional[Any] = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.details = details

    def __str__(self) -> str:
        code_str = f" [HTTP {self.status_code}]" if self.status_code else ""
        return f"FirefliesAPIError{code_str}: {self.message}"


class FirefliesClient:
    """
    Dedicated Asynchronous Fireflies.ai GraphQL Client for FS-backend.
    Handles:
    1. Querying transcripts by transcript ID.
    2. Submitting raw media links via the uploadAudio GraphQL mutation.
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        api_key: Optional[str] = None,
        api_url: Optional[str] = None,
        timeout: float = 45.0,
    ):
        self.settings = settings or get_settings()
        self.api_key = api_key or self.settings.fireflies_api_key
        self.api_url = api_url or self.settings.fireflies_api_url
        self.timeout = timeout

    async def _execute_graphql(
        self,
        query: str,
        variables: Dict[str, Any],
        max_retries: int = 3,
        initial_backoff: float = 1.0,
    ) -> Dict[str, Any]:
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
                    raise FirefliesAPIError(
                        f"Fireflies returned HTTP {response.status_code}: {response.text}",
                        status_code=response.status_code,
                    )

                data = response.json()
                if "errors" in data and data["errors"]:
                    err_msg = "; ".join([e.get("message", "Unknown error") for e in data["errors"]])
                    raise FirefliesAPIError(
                        f"GraphQL Error from Fireflies: {err_msg}",
                        status_code=400,
                        details=data["errors"],
                    )

                return data.get("data", {})

            except (httpx.TimeoutException, httpx.RequestError) as exc:
                if attempt >= max_retries:
                    logger.error(f"Fireflies API network failure after {max_retries} attempts: {exc}")
                    raise FirefliesAPIError(
                        f"Connection to Fireflies API failed after {max_retries} retries: {str(exc)}",
                        status_code=504 if isinstance(exc, httpx.TimeoutException) else 503,
                    ) from exc
                logger.warning(f"Network error on attempt {attempt}/{max_retries}: {exc}. Retrying in {backoff}s...")
                await asyncio.sleep(backoff)
                backoff *= 2.0

        raise FirefliesAPIError("Max retries exceeded communicating with Fireflies.", status_code=500)

    async def fetch_transcript_by_id(self, transcript_id: str) -> Dict[str, Any]:
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
            raise FirefliesAPIError(f"Transcript with ID '{transcript_id}' not found in Fireflies.", status_code=404)

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
            "speakers": speakers,
            "sentences": sentences,
            "raw_payload": transcript_data,
        }

    async def upload_audio_from_url(self, audio_url: str, title: Optional[str] = None) -> Dict[str, Any]:
        """
        Submit an external raw media recording URL (mp3, wav, mp4, m4a) to Fireflies for async transcription.

        Args:
            audio_url: Public direct link to audio/video file.
            title: Optional custom meeting title.

        Returns:
            dict with uploadAudio mutation confirmation.
        """
        logger.info(f"Submitting audio URL to Fireflies uploadAudio mutation: '{audio_url}' (Title: {title})...")
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
