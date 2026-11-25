import contextlib
import json
import logging
from collections.abc import Iterable, Iterator
from typing import Any

import httpx
from config import frontend_config

logger = logging.getLogger(__name__)

TOKEN_KEY = "auth_token"
SESSION_EXPIRED_KEY = "session_expired"


def _session_state() -> Any:
    """Streamlit's per-browser-session state, or None when running outside Streamlit (tests, scripts)."""
    try:
        import streamlit as st

        return st.session_state
    except Exception:
        return None


def _error_detail(response: httpx.Response) -> str:
    """Readable message from an API error body (handles both string and validation-error details)."""
    try:
        detail = response.json().get("detail", response.text)
    except Exception:
        return response.text
    if isinstance(detail, list):
        return "; ".join(str(d.get("msg", d)).removeprefix("Value error, ") for d in detail)
    return str(detail)


def _iter_sse(lines: Iterable[str]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Parses server-sent-event lines into (event name, JSON data); events without valid JSON data are skipped."""
    event, data = "message", []
    for line in lines:
        if line == "":
            if data:
                with contextlib.suppress(ValueError):
                    yield event, json.loads("\n".join(data))
            event, data = "message", []
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:  # the stream ended without the blank line that closes an event
        with contextlib.suppress(ValueError):
            yield event, json.loads("\n".join(data))


class MeetingIntelligenceClient:
    """
    HTTP Client for communicating with the FastAPI Meeting Intelligence backend.
    Handles Dual-Mode query routing requests, health probes, and structured error recovery.
    """

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float | None = None
    ):
        self._custom_base_url = base_url
        self._custom_timeout = timeout

    @property
    def base_url(self) -> str:
        return (self._custom_base_url or frontend_config.FASTAPI_BACKEND_URL).rstrip("/")

    @property
    def timeout(self) -> float:
        return self._custom_timeout or frontend_config.REQUEST_TIMEOUT

    @property
    def query_url(self) -> str:
        return f"{self.base_url}{frontend_config.QUERY_ENDPOINT}"

    @property
    def query_stream_url(self) -> str:
        return f"{self.query_url}/stream"

    @property
    def meetings_api_url(self) -> str:
        return f"{self.base_url}/api/v1/meetings"

    def _auth_headers(self) -> dict[str, str]:
        """Bearer token of the logged-in user (kept in that browser session only, never on the shared client)."""
        state = _session_state()
        token = state.get(TOKEN_KEY) if state is not None else None
        return {"Authorization": f"Bearer {token}"} if token else {}

    def _expire_session(self) -> None:
        """The backend rejected our token: drop the login so the app shows the login page again."""
        state = _session_state()
        if state is not None:
            state.pop(TOKEN_KEY, None)
            state[SESSION_EXPIRED_KEY] = True

    def _authenticate(self, path: str, email: str, password: str) -> dict[str, Any]:
        """POST credentials to /auth/<path>; returns {"success", "token", "user"} or {"success": False, "error"}."""
        if not self.base_url:
            return {"success": False, "error": "FASTAPI_BACKEND_URL is not configured."}
        try:
            with httpx.Client(timeout=min(self.timeout, 20.0)) as client:
                res = client.post(f"{self.base_url}/api/v1/auth/{path}", json={"email": email, "password": password})
        except Exception as e:
            return {"success": False, "error": f"Cannot reach the backend: {e}"}
        if res.status_code in (200, 201):
            body = res.json()
            return {"success": True, "token": body["access_token"], "user": body["user"]}
        return {"success": False, "error": _error_detail(res)}

    def login(self, email: str, password: str) -> dict[str, Any]:
        return self._authenticate("login", email, password)

    def signup(self, email: str, password: str) -> dict[str, Any]:
        return self._authenticate("signup", email, password)

    @staticmethod
    def _query_payload(
        prompt: str, project_id: str | None, speaker: str | None, meeting_id: str | None, history: list[dict[str, str]] | None
    ) -> dict[str, Any]:
        return {
            "query": prompt.strip(),
            "project_id": project_id if project_id else None,
            "speaker": speaker if speaker else None,
            "meeting_id": meeting_id if meeting_id else None,
            "history": history or [],
        }

    @staticmethod
    def _query_result(data: dict[str, Any], prompt: str) -> dict[str, Any]:
        """The answer dict the UI works with, from the backend's query response."""
        return {
            "success": True,
            "query": data.get("query", prompt),
            "routing_mode": data.get("routing_mode", "UNKNOWN"),
            "routing_reasoning": data.get("routing_reasoning", ""),
            "answer": data.get("answer", "No answer provided by the model."),
            "citations": data.get("citations", []),
            "citations_count": data.get("citations_count", 0),
            "provenance_verified": data.get("provenance_verified", False),
            "macro_data": data.get("macro_data"),
            "llm_model_used": data.get("llm_model_used", "gpt-4o-mini")
        }

    def query_backend(
        self,
        prompt: str,
        project_id: str | None = None,
        speaker: str | None = None,
        meeting_id: str | None = None,
        history: list[dict[str, str]] | None = None,
        **kwargs
    ) -> dict[str, Any]:
        """
        Sends natural language prompt and optional filters to POST /api/v1/query.
        Returns structured dictionary containing answer, citations, and routing metadata.
        """
        if not self.base_url:
            return {
                "success": False,
                "error": "FASTAPI_BACKEND_URL is not configured in environment or Streamlit secrets.",
                "answer": "⚠️ **Configuration Required**: `FASTAPI_BACKEND_URL` is not set. Please set it in Streamlit Secrets.",
                "citations": []
            }
        payload = self._query_payload(prompt, project_id, speaker, meeting_id, history)

        try:
            with httpx.Client(timeout=self.timeout) as client:
                logger.info(f"[APIClient] Sending query to {self.query_url}: '{prompt}'")
                response = client.post(
                    self.query_url,
                    json=payload,
                    headers={"Content-Type": "application/json", **self._auth_headers()}
                )

                if response.status_code == 200:
                    return self._query_result(response.json(), prompt)
                else:
                    if response.status_code == 401:
                        self._expire_session()
                    error_detail = response.text
                    with contextlib.suppress(Exception):
                        error_detail = response.json().get("detail", response.text)
                    return {
                        "success": False,
                        "error": f"Backend Error (HTTP {response.status_code}): {error_detail}",
                        "answer": f"⚠️ An error occurred while communicating with the backend: {error_detail}",
                        "citations": []
                    }

        except httpx.ConnectError:
            return {
                "success": False,
                "error": f"Connection Refused: Cannot reach backend at {self.query_url}",
                "answer": f"🔌 **Backend Disconnected**: Please ensure the backend server is running and reachable at `{self.base_url}`.",
                "citations": []
            }
        except httpx.TimeoutException:
            return {
                "success": False,
                "error": f"Request Timed Out after {self.timeout}s",
                "answer": "⏱️ **Request Timed Out**: The backend took longer than expected to process your query.",
                "citations": []
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "answer": f"⚠️ Unexpected error: {e!s}",
                "citations": []
            }

    def query_backend_stream(
        self,
        prompt: str,
        project_id: str | None = None,
        speaker: str | None = None,
        meeting_id: str | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """
        Like query_backend, but yields events while the answer is written (POST /api/v1/query/stream):
        {"event": "status", "message"}, {"event": "token", "text"}, then one {"event": "final", "data"} whose data is
        the dict query_backend returns. A failure is a single {"event": "error", **the failure dict query_backend
        returns, "status_code"} (status_code is None when the backend could not be reached).
        """
        def failure(error: str, answer: str, status_code: int | None = None) -> dict[str, Any]:
            return {"event": "error", "success": False, "error": error, "answer": answer, "citations": [], "status_code": status_code}

        if not self.base_url:
            yield failure(
                "FASTAPI_BACKEND_URL is not configured in environment or Streamlit secrets.",
                "⚠️ **Configuration Required**: `FASTAPI_BACKEND_URL` is not set. Please set it in Streamlit Secrets.",
            )
            return
        payload = self._query_payload(prompt, project_id, speaker, meeting_id, history)

        try:
            # the timeout applies to each wait for the next piece of the stream, not to the whole answer
            with httpx.Client(timeout=self.timeout) as client:
                logger.info(f"[APIClient] Streaming query to {self.query_stream_url}: '{prompt}'")
                with client.stream(
                    "POST", self.query_stream_url, json=payload, headers={"Content-Type": "application/json", **self._auth_headers()}
                ) as response:
                    if response.status_code != 200:
                        response.read()
                        if response.status_code == 401:
                            self._expire_session()
                        detail = _error_detail(response)
                        yield failure(
                            f"Backend Error (HTTP {response.status_code}): {detail}",
                            f"⚠️ An error occurred while communicating with the backend: {detail}",
                            response.status_code,
                        )
                        return
                    finished = False
                    for event, data in _iter_sse(response.iter_lines()):
                        if event == "status":
                            yield {"event": "status", "message": data.get("message", "")}
                        elif event == "token":
                            yield {"event": "token", "text": data.get("text", "")}
                        elif event == "final":
                            finished = True
                            yield {"event": "final", "data": self._query_result(data, prompt)}
                        elif event == "error":
                            finished = True
                            message = data.get("message", "The answer could not be completed.")
                            yield failure(message, f"⚠️ {message}")
                    if not finished:
                        yield failure(
                            "The answer stream ended before it was complete.",
                            "⚠️ The answer was interrupted before it was complete. Please try again.",
                        )
        except httpx.ConnectError:
            yield failure(
                f"Connection Refused: Cannot reach backend at {self.query_stream_url}",
                f"🔌 **Backend Disconnected**: Please ensure the backend server is running and reachable at `{self.base_url}`.",
            )
        except httpx.TimeoutException:
            yield failure(
                f"Request Timed Out after {self.timeout}s",
                "⏱️ **Request Timed Out**: The backend took longer than expected to process your query.",
            )
        except Exception as e:
            yield failure(str(e), f"⚠️ Unexpected error: {e!s}")

    def list_projects(self) -> list[str]:
        """Fetches the list of all distinct project IDs."""
        if not self.base_url:
            return []
        endpoint = f"{self.meetings_api_url}/projects"
        try:
            with httpx.Client(timeout=min(self.timeout, 15.0)) as client:
                res = client.get(endpoint, headers=self._auth_headers())
                if res.status_code == 401:
                    self._expire_session()
                if res.status_code == 200:
                    return res.json().get("projects", [])
                logger.warning(f"Error fetching projects list from {endpoint}: HTTP {res.status_code}")
        except Exception as e:
            logger.warning(f"Error fetching projects list from {endpoint}: {e}")
        return []

    def list_meetings(self) -> list[dict[str, Any]]:
        """Fetches the list of all ingested meetings."""
        if not self.base_url:
            return []
        endpoint = f"{self.meetings_api_url}/"
        try:
            with httpx.Client(timeout=min(self.timeout, 20.0)) as client:
                res = client.get(endpoint, headers=self._auth_headers())
                if res.status_code == 401:
                    self._expire_session()
                if res.status_code == 200:
                    return res.json().get("meetings", [])
                logger.warning(f"Error fetching meetings list from {endpoint}: HTTP {res.status_code}")
        except Exception as e:
            logger.warning(f"Error fetching meetings list from {endpoint}: {e}")
        return []

    def get_meeting(self, meeting_id: str) -> dict[str, Any] | None:
        """Fetches meeting details and Level 1 turns."""
        if not self.base_url:
            return None
        endpoint = f"{self.meetings_api_url}/{meeting_id}"
        try:
            with httpx.Client(timeout=min(self.timeout, 25.0)) as client:
                res = client.get(endpoint, headers=self._auth_headers())
                if res.status_code == 401:
                    self._expire_session()
                if res.status_code == 200:
                    return res.json()
                logger.warning(f"Error fetching meeting {meeting_id} from {endpoint}: HTTP {res.status_code}")
        except Exception as e:
            logger.warning(f"Error fetching meeting {meeting_id} from {endpoint}: {e}")
        return None

    def ingest_link(self, link: str, project_id: str | None = None, title: str | None = None) -> dict[str, Any]:
        """Submits meeting link (Fireflies dashboard or media recording) for asynchronous ingestion."""
        if not self.base_url:
            return {"success": False, "error": "FASTAPI_BACKEND_URL is not configured in environment or Streamlit secrets."}
        payload = {"link": link.strip()}
        if project_id and project_id.strip():
            payload["project_id"] = project_id.strip()
        if title and title.strip():
            payload["title"] = title.strip()

        endpoint = f"{self.meetings_api_url}/ingest-link"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                res = client.post(endpoint, json=payload, headers=self._auth_headers())
                if res.status_code in (200, 201, 202):
                    return {"success": True, "data": res.json()}
                if res.status_code == 401:
                    self._expire_session()
                return {"success": False, "error": _error_detail(res)}
        except Exception as e:
            return {"success": False, "error": f"{e} (Target: {endpoint})"}


# Singleton client instance
api_client = MeetingIntelligenceClient()
