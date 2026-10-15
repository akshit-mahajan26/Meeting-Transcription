"""Ownership rules: project IDs are scoped per account, and meeting IDs are unique per account."""

from sqlalchemy.orm import Session

from ..core.database import MeetingModel, ProjectModel


class OwnershipError(Exception):
    """Raised when an ID is already owned by a different account."""


def scope_project_id(user_id: str, project_id: str | None) -> str | None:
    """Namespace a user-facing project ID to the user's account for internal DB storage.

    Format: '{user_id}:{clean_project_id}'
    """
    if not project_id:
        return None
    clean = project_id.strip()
    if not clean:
        return None
    prefix = f"{user_id}:"
    if clean.startswith(prefix):
        return clean
    # projects.id is String(100); 36 chars UUID + 1 char ':' leaves up to 63 chars for project name
    return f"{prefix}{clean[:63]}"


def unscope_project_id(scoped_project_id: str | None, user_id: str | None = None) -> str | None:
    """Convert an internal namespaced project ID back to the user-facing name.

    E.g., '2ff8b249-...:marketing' -> 'marketing'
    """
    if not scoped_project_id:
        return None
    if user_id:
        prefix = f"{user_id}:"
        if scoped_project_id.startswith(prefix):
            return scoped_project_id[len(prefix):]
    if ":" in scoped_project_id:
        parts = scoped_project_id.split(":", 1)
        if len(parts[0]) == 36:  # Standard UUID length
            return parts[1]
    return scoped_project_id


def resolve_project_id(db: Session, user_id: str, project_id: str | None) -> str | None:
    """Resolves a user-facing project ID to the matching database ID (scoped or legacy)."""
    if not project_id or not project_id.strip():
        return None
    clean = project_id.strip()
    scoped = scope_project_id(user_id, clean)

    # 1. Match scoped ID for this user
    if scoped and db.query(ProjectModel).filter(ProjectModel.id == scoped, ProjectModel.user_id == user_id).first():
        return scoped
    # 2. Match legacy un-scoped ID if owned by this user
    if db.query(ProjectModel).filter(ProjectModel.id == clean, ProjectModel.user_id == user_id).first():
        return clean
    return scoped


def meeting_taken(db: Session, user_id: str, meeting_id: str) -> bool:
    """True if the meeting ID exists and belongs to someone else."""
    meeting = db.query(MeetingModel).filter(MeetingModel.id == meeting_id).first()
    return meeting is not None and meeting.user_id != user_id


def get_owned_meeting(db: Session, user_id: str, meeting_id: str) -> MeetingModel | None:
    """The account's meeting, or None if new. Refuses an ID owned by another account."""
    meeting = db.query(MeetingModel).filter(MeetingModel.id == meeting_id).first()
    if meeting is not None and meeting.user_id != user_id:
        raise OwnershipError(f"Meeting '{meeting_id}' belongs to another account.")
    return meeting


def claim_project(db: Session, user_id: str, project_id: str) -> ProjectModel:
    """Returns the account's project, creating it if new.

    Uses namespaced project IDs (e.g., '{user_id}:{clean_id}') so multiple
    accounts can have projects with the same display name/ID without collision.
    """
    clean_name = unscope_project_id(project_id, user_id) or project_id.strip()
    scoped_id = scope_project_id(user_id, clean_name)

    # Check for existing scoped project
    project = db.query(ProjectModel).filter(
        ProjectModel.id == scoped_id,
        ProjectModel.user_id == user_id,
    ).first()

    # Fallback to check for existing legacy unscoped project for this user
    if project is None:
        project = db.query(ProjectModel).filter(
            ProjectModel.id == clean_name,
            ProjectModel.user_id == user_id,
        ).first()

    if project is None:
        project = ProjectModel(
            id=scoped_id,
            user_id=user_id,
            name=clean_name,
        )
        db.add(project)
        db.flush()

    return project

