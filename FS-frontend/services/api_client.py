import sys
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List
import httpx

# Ensure paths are resolved dynamically
ROOT_DIR = Path(__file__).resolve().parent.parent.parent
FRONTEND_DIR = Path(__file__).resolve().parent.parent
for p in (str(ROOT_DIR), str(FRONTEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

try:
    from config import frontend_config
except ModuleNotFoundError:
    try:
        from FS_frontend.config import frontend_config
    except ModuleNotFoundError:
        from frontend.config import frontend_config

logger = logging.getLogger(__name__)


class MeetingIntelligenceClient:
    """
    HTTP Client for communicating with the FastAPI Meeting Intelligence backend.
    Handles Dual-Mode query routing requests, health probes, and structured error recovery.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: Optional[float] = None
    ):
        self.base_url = (base_url or frontend_config.FASTAPI_BACKEND_URL).rstrip("/")
        self.timeout = timeout or frontend_config.REQUEST_TIMEOUT
        self.query_url = f"{self.base_url}{frontend_config.QUERY_ENDPOINT}"
        self.health_url = f"{self.base_url}{frontend_config.HEALTH_ENDPOINT}"
        self.meetings_api_url = f"{self.base_url}/api/v1/meetings"

    def check_health(self) -> Dict[str, Any]:
        """
        Probes backend health check endpoint.
        Returns a dict with 'is_healthy': bool, and error details if unavailable.
        """
        try:
            with httpx.Client(timeout=3.0) as client:
                response = client.get(self.health_url)
                if response.status_code == 200:
                    return {"is_healthy": True, "details": response.json()}
                return {
                    "is_healthy": False,
                    "error": f"Server responded with status HTTP {response.status_code}"
                }
        except httpx.ConnectError:
            return {
                "is_healthy": False,
                "error": f"Could not connect to backend at {self.base_url}. Ensure FastAPI is running."
            }
        except Exception as e:
            return {
                "is_healthy": False,
                "error": f"Health check failed: {str(e)}"
            }

    def query_backend(
        self,
        prompt: str,
        project_id: Optional[str] = None,
        speaker: Optional[str] = None,
        meeting_id: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Sends natural language prompt and optional filters to POST /api/v1/query.
        Returns structured dictionary containing answer, citations, and routing metadata.
        """
        payload = {
            "query": prompt.strip(),
            "project_id": project_id if project_id else None,
            "speaker": speaker if speaker else None,
            "meeting_id": meeting_id if meeting_id else None
        }

        try:
            with httpx.Client(timeout=self.timeout) as client:
                logger.info(f"[APIClient] Sending query to {self.query_url}: '{prompt}'")
                response = client.post(
                    self.query_url,
                    json=payload,
                    headers={"Content-Type": "application/json"}
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
                    error_detail = response.text
                    try:
                        error_detail = response.json().get("detail", response.text)
                    except Exception:
                        pass
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
                "answer": "🔌 **Backend Disconnected**: Please ensure the FastAPI server is running on `http://localhost:8000`.",
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
                "answer": f"⚠️ Unexpected error: {str(e)}",
                "citations": []
            }

    def list_projects(self) -> List[str]:
        """Fetches the list of all distinct project IDs."""
        try:
            with httpx.Client(timeout=5.0) as client:
                res = client.get(f"{self.meetings_api_url}/projects")
                if res.status_code == 200:
                    return res.json().get("projects", [])
        except Exception as e:
            logger.warning(f"Error fetching projects list: {e}")
        return []

    def list_meetings(self) -> List[Dict[str, Any]]:
        """Fetches the list of all ingested meetings."""
        try:
            with httpx.Client(timeout=5.0) as client:
                res = client.get(f"{self.meetings_api_url}/")
                if res.status_code == 200:
                    return res.json().get("meetings", [])
        except Exception as e:
            logger.warning(f"Error fetching meetings list: {e}")
        return []

    def get_meeting(self, meeting_id: str) -> Optional[Dict[str, Any]]:
        """Fetches meeting details and Level 1 turns."""
        try:
            with httpx.Client(timeout=8.0) as client:
                res = client.get(f"{self.meetings_api_url}/{meeting_id}")
                if res.status_code == 200:
                    return res.json()
        except Exception as e:
            logger.warning(f"Error fetching meeting {meeting_id}: {e}")
        return None

    def ingest_raw_transcript(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Ingests raw transcript sentences for normalization and storage."""
        with httpx.Client(timeout=30.0) as client:
            res = client.post(f"{self.meetings_api_url}/ingest/raw", json=payload)
            if res.status_code in (200, 201):
                return {"success": True, "data": res.json()}
            return {"success": False, "error": f"HTTP {res.status_code}: {res.text}"}

    def ingest_link(self, link: str, project_id: Optional[str] = None, title: Optional[str] = None) -> Dict[str, Any]:
        """Submits meeting link (Fireflies dashboard or media recording) for asynchronous ingestion."""
        payload = {"link": link.strip()}
        if project_id and project_id.strip():
            payload["project_id"] = project_id.strip()
        if title and title.strip():
            payload["title"] = title.strip()

        try:
            with httpx.Client(timeout=15.0) as client:
                res = client.post(f"{self.meetings_api_url}/ingest-link", json=payload)
                if res.status_code in (200, 201, 202):
                    return {"success": True, "data": res.json()}
                return {"success": False, "error": f"HTTP {res.status_code}: {res.text}"}
        except Exception as e:
            return {"success": False, "error": str(e)}


# Singleton client instance
api_client = MeetingIntelligenceClient()


def query_backend(prompt: str, project_id: Optional[str] = None, speaker: Optional[str] = None, meeting_id: Optional[str] = None) -> Dict[str, Any]:
    """Convenience functional interface for querying the backend."""
    return api_client.query_backend(prompt, project_id=project_id, speaker=speaker, meeting_id=meeting_id)
