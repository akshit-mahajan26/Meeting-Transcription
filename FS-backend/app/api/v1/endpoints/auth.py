from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ....api.deps import get_current_user
from ....core.config import get_settings
from ....core.database import UserModel, get_db
from ....core.ratelimit import (
    clear_login_failures,
    limit_by_ip,
    login_throttled,
    login_throttled_error,
    record_login_failure,
)
from ....core.security import create_access_token, hash_password, verify_password
from ....schemas.auth import Credentials, TokenResponse, UserResponse

router = APIRouter()

# Verified when the email is unknown so a login takes the same time either way
_DUMMY_HASH = hash_password("not-a-real-password")


def _token_for(user: UserModel) -> TokenResponse:
    return TokenResponse(access_token=create_access_token(user.id), user=UserResponse.model_validate(user))


@router.post(
    "/signup",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account",
)
def signup(
    payload: Credentials, db: Session = Depends(get_db), _limit: None = Depends(limit_by_ip("rate_limit_signup_ip"))
) -> TokenResponse:
    if not get_settings().allow_signup:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Sign-up is disabled on this server.")
    if db.query(UserModel).filter(UserModel.email == payload.email).first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="An account with this email already exists.")
    user = UserModel(email=payload.email, password_hash=hash_password(payload.password))
    db.add(user)
    db.flush()
    try:
        db.commit()
    except IntegrityError as err:  # two signups racing on the same email
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="An account with this email already exists."
        ) from err
    db.refresh(user)
    return _token_for(user)


def _check_login(payload: Credentials, db: Session) -> UserModel | None:
    user = db.query(UserModel).filter(UserModel.email == payload.email).first()
    password_ok = verify_password(payload.password, user.password_hash if user else _DUMMY_HASH)
    return user if user is not None and password_ok else None


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Log in and receive an access token",
    dependencies=[Depends(limit_by_ip("rate_limit_login_ip"))],
)
async def login(payload: Credentials, db: Session = Depends(get_db)) -> TokenResponse:
    retry_after = await login_throttled(payload.email)
    if retry_after is not None:
        raise login_throttled_error(retry_after)
    user = await run_in_threadpool(_check_login, payload, db)
    if user is None:
        await record_login_failure(payload.email)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password.")
    await clear_login_failures(payload.email)
    return await run_in_threadpool(_token_for, user)


def _sign_out_everywhere(user_id: str, db: Session) -> None:
    db.query(UserModel).filter(UserModel.id == user_id).update({"tokens_valid_after": datetime.now(UTC)})
    db.commit()


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Log out (signs the user out on every device)",
)
async def logout(user: UserModel = Depends(get_current_user), db: Session = Depends(get_db)) -> None:
    """Invalidates every token issued to the user so far, so this signs them out on every device."""
    await run_in_threadpool(_sign_out_everywhere, user.id, db)


@router.post(
    "/logout-all",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Log out on every device",
)
async def logout_all(user: UserModel = Depends(get_current_user), db: Session = Depends(get_db)) -> None:
    """Invalidates every token issued to the user so far (same effect as /logout)."""
    await run_in_threadpool(_sign_out_everywhere, user.id, db)


@router.get("/me", response_model=UserResponse, summary="The authenticated user")
def me(user: UserModel = Depends(get_current_user)) -> UserResponse:
    return UserResponse.model_validate(user)
