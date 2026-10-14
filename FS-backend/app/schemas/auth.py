import re

from pydantic import BaseModel, Field, field_validator

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LENGTH = 6
MAX_PASSWORD_LENGTH = 72  # bcrypt ignores everything after 72 bytes


class Credentials(BaseModel):
    """Email + password body shared by signup and login."""
    email: str = Field(..., max_length=255)
    password: str = Field(..., min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        email = v.strip().lower()
        if not EMAIL_PATTERN.match(email):
            raise ValueError("Enter a valid email address.")
        return email


class UserResponse(BaseModel):
    id: str
    email: str

    model_config = {"from_attributes": True}


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse
