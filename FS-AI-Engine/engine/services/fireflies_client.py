import logging
from typing import Optional, Dict, Any, List
import httpx
from ..core.config import settings, Settings
from ..models.schemas import RawSentence, RawTranscriptPayload

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

# Fallback query if 'speakers' or other nested fields are not permitted on certain API keys
TRANSCRIPT_QUERY_COMPACT = """
query GetTranscript($transcriptId: String!) {
  transcript(id: $transcriptId) {
    id
    title
    date
    duration
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


class FirefliesAPIError(Exception):
    """Exception raised when Fireflies GraphQL API returns an error or fails."""
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
    Dedicated Asynchronous Fireflies.ai GraphQL Client.
    Communicates securely with the Fireflies GraphQL API to fetch full meeting transcripts,
    speaker details, sentence timestamps, and meeting metadata.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_url: Optional[str] = None,
        timeout: float = 45.0,
    ):
        self.api_key = api_key or settings.FIREFLIES_API_KEY
        self.api_url = api_url or settings.FIREFLIES_API_URL
        self.timeout = timeout

    async def fetch_transcript_by_id(
        self,
        meeting_id: str,
        api_key_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Asynchronously fetch the complete meeting transcript from the Fireflies GraphQL API.

        Args:
            meeting_id: Unique Fireflies transcript ID.
            api_key_override: Optional per-request API key.

        Returns:
            dict containing:
                - 'id': str
                - 'title': str
                - 'date': str
                - 'duration': float
                - 'speakers': List[Dict[str, str]]
                - 'sentences': List[Dict[str, Any]]
                - 'raw_payload': Dict[str, Any]

        Raises:
            FirefliesAPIError: If unauthorized, not found, GraphQL error, or connection failure.
        """
        active_key = api_key_override or self.api_key
        if not active_key or not active_key.strip():
            logger.error("Attempted to call Fireflies API without FIREFLIES_API_KEY configured.")
            raise FirefliesAPIError(
                "Fireflies API key is required. Set FIREFLIES_API_KEY in environment or provide it in the request.",
                status_code=401,
            )

        headers = {
            "Authorization": f"Bearer {active_key.strip()}",
            "Content-Type": "application/json",
        }
        payload = {
            "query": TRANSCRIPT_QUERY,
            "variables": {"transcriptId": meeting_id},
        }

        logger.info(f"Querying Fireflies GraphQL API for meeting ID: '{meeting_id}'...")

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(self.api_url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            logger.error(f"Fireflies API connection timed out ({self.timeout}s): {exc}")
            raise FirefliesAPIError(
                f"Connection to Fireflies API timed out after {self.timeout} seconds.",
                status_code=504,
            ) from exc
        except httpx.RequestError as exc:
            logger.error(f"Network error communicating with Fireflies API: {exc}")
            raise FirefliesAPIError(
                f"Failed to connect to Fireflies GraphQL API: {str(exc)}",
                status_code=503,
            ) from exc

        if response.status_code != 200:
            logger.error(f"Fireflies API returned HTTP status {response.status_code}: {response.text}")
            raise FirefliesAPIError(
                f"Fireflies API returned HTTP {response.status_code}: {response.text}",
                status_code=response.status_code,
            )

        result_json = response.json()

        # Handle GraphQL Errors
        if "errors" in result_json and result_json["errors"]:
            # Check if failure was due to 'speakers' field schema restriction and retry with compact query
            err_text = "; ".join([e.get("message", "Unknown error") for e in result_json["errors"]])
            if "speakers" in err_text.lower():
                logger.warning("Field 'speakers' not permitted; retrying with compact transcript query.")
                return await self._fetch_compact_transcript(client_meeting_id=meeting_id, headers=headers)

            logger.error(f"GraphQL error from Fireflies: {err_text}")
            raise FirefliesAPIError(
                f"GraphQL Error from Fireflies: {err_text}",
                status_code=400,
                details=result_json["errors"],
            )

        transcript_data = result_json.get("data", {}).get("transcript")
        if not transcript_data:
            logger.warning(f"No transcript found in Fireflies with ID: '{meeting_id}'")
            raise FirefliesAPIError(
                f"Transcript with ID '{meeting_id}' not found.",
                status_code=404,
            )

        # Extract speakers list or derive distinct speakers from sentences
        raw_speakers = transcript_data.get("speakers") or []
        raw_sentences = transcript_data.get("sentences") or []
        if not raw_speakers and raw_sentences:
            seen = set()
            for s in raw_sentences:
                name = s.get("speaker_name") or "Unknown Speaker"
                sid = s.get("speaker_id") or name
                if sid not in seen:
                    seen.add(sid)
                    raw_speakers.append({"id": sid, "name": name})

        parsed_dict = {
            "id": str(transcript_data.get("id") or meeting_id),
            "title": str(transcript_data.get("title") or f"Meeting {meeting_id}"),
            "date": str(transcript_data.get("date")) if transcript_data.get("date") else None,
            "duration": float(transcript_data.get("duration")) if transcript_data.get("duration") is not None else 0.0,
            "speakers": raw_speakers,
            "sentences": raw_sentences,
            "raw_payload": transcript_data,
        }

        logger.info(
            f"Successfully fetched transcript '{meeting_id}' from Fireflies "
            f"({len(raw_sentences)} sentences, {len(raw_speakers)} speakers)."
        )
        return parsed_dict

    async def _fetch_compact_transcript(self, client_meeting_id: str, headers: Dict[str, str]) -> Dict[str, Any]:
        """Retry fallback using compact GraphQL query without optional fields."""
        payload = {
            "query": TRANSCRIPT_QUERY_COMPACT,
            "variables": {"transcriptId": client_meeting_id},
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(self.api_url, json=payload, headers=headers)
        if resp.status_code != 200:
            raise FirefliesAPIError(f"HTTP {resp.status_code}: {resp.text}", status_code=resp.status_code)
        data = resp.json()
        if "errors" in data and data["errors"]:
            err_msg = "; ".join([e.get("message", "") for e in data["errors"]])
            raise FirefliesAPIError(f"GraphQL Error: {err_msg}", status_code=400)
        t = data.get("data", {}).get("transcript")
        if not t:
            raise FirefliesAPIError(f"Transcript '{client_meeting_id}' not found.", status_code=404)
        sentences = t.get("sentences") or []
        speakers = []
        seen = set()
        for s in sentences:
            name = s.get("speaker_name") or "Unknown Speaker"
            sid = s.get("speaker_id") or name
            if sid not in seen:
                seen.add(sid)
                speakers.append({"id": sid, "name": name})
        return {
            "id": str(t.get("id") or client_meeting_id),
            "title": str(t.get("title") or f"Meeting {client_meeting_id}"),
            "date": str(t.get("date")) if t.get("date") else None,
            "duration": float(t.get("duration")) if t.get("duration") is not None else 0.0,
            "speakers": speakers,
            "sentences": sentences,
            "raw_payload": t,
        }

    async def fetch_raw_transcript_payload(
        self,
        meeting_id: str,
        api_key_override: Optional[str] = None,
    ) -> RawTranscriptPayload:
        """
        Convenience method that queries the GraphQL API and converts the result
        directly into the typed RawTranscriptPayload expected by PipelineOrchestrator.
        """
        data = await self.fetch_transcript_by_id(meeting_id=meeting_id, api_key_override=api_key_override)
        raw_sentences: List[RawSentence] = []
        for s in data.get("sentences", []):
            raw_sentences.append(
                RawSentence(
                    index=int(s.get("index", 0)),
                    speaker_name=s.get("speaker_name") or "Unknown Speaker",
                    speaker_id=s.get("speaker_id"),
                    text=(s.get("text") or "").strip(),
                    raw_text=s.get("raw_text"),
                    start_time=float(s.get("start_time", 0.0)),
                    end_time=float(s.get("end_time", 0.0)),
                )
            )

        return RawTranscriptPayload(
            meeting_id=data["id"],
            title=data.get("title"),
            date=data.get("date"),
            duration=data.get("duration"),
            sentences=raw_sentences,
        )


fireflies_client = FirefliesClient()


def get_fireflies_client() -> FirefliesClient:
    return fireflies_client
