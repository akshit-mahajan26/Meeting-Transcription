from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ....api.deps import get_current_user
from ....core.database import UserModel, get_db
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
def signup(payload: Credentials, db: Session = Depends(get_db)) -> TokenResponse:
    if db.query(UserModel).filter(UserModel.email == payload.email).first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="An account with this email already exists.")
    user = UserModel(email=payload.email, password_hash=hash_password(payload.password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError as err:  # two signups racing on the same email
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="An account with this email already exists."
        ) from err
    db.refresh(user)
    return _token_for(user)


@router.post("/login", response_model=TokenResponse, summary="Log in and receive an access token")
def login(payload: Credentials, db: Session = Depends(get_db)) -> TokenResponse:
    user = db.query(UserModel).filter(UserModel.email == payload.email).first()
    password_ok = verify_password(payload.password, user.password_hash if user else _DUMMY_HASH)
    if user is None or not password_ok:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password.")
    return _token_for(user)


@router.get("/me", response_model=UserResponse, summary="The authenticated user")
def me(user: UserModel = Depends(get_current_user)) -> UserResponse:
    return UserResponse.model_validate(user)
