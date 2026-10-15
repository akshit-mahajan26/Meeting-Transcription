import contextlib
import logging
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
        payload = {
            "query": prompt.strip(),
            "project_id": project_id if project_id else None,
            "speaker": speaker if speaker else None,
            "meeting_id": meeting_id if meeting_id else None,
            "history": history or [],
        }

        try:
            with httpx.Client(timeout=self.timeout) as client:
                logger.info(f"[APIClient] Sending query to {self.query_url}: '{prompt}'")
                response = client.post(
                    self.query_url,
                    json=payload,
                    headers={"Content-Type": "application/json", **self._auth_headers()}
                )

                if response.status_code == 200:
                    data = response.json()
                    return {
                        "success": True,
                        "query": data.get("query", prompt),
                        "routing_mode": data.get("routing_mode", "UNKNOWN"),
                        "routing_confidence": data.get("routing_confidence", 1.0),
                        "routing_reasoning": data.get("routing_reasoning", ""),
                        "answer": data.get("answer", "No answer provided by the model."),
                        "citations": data.get("citations", []),
                        "citations_count": data.get("citations_count", 0),
                        "provenance_verified": data.get("provenance_verified", False),
                        "macro_data": data.get("macro_data"),
                        "llm_model_used": data.get("llm_model_used", "gpt-4o-mini")
                    }
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
