"""Recruiter signup and login."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.auth import create_access_token, hash_password, verify_password
from app.models import User

router = APIRouter(prefix="/auth", tags=["auth"])


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=72)

    @field_validator("email")
    @classmethod
    def normalize(cls, value: str) -> str:
        value = value.strip().lower()
        if "@" not in value:
            raise ValueError("invalid email")
        return value


class TokenResponse(BaseModel):
    access_token: str
    user_id: int
    email: str


@router.post("/signup", response_model=TokenResponse, status_code=201)
async def signup(body: Credentials, request: Request):
    settings = request.app.state.settings
    user = User(email=body.email, password_hash=await hash_password(body.password, settings.bcrypt_rounds))
    async with request.app.state.session_factory() as session:
        session.add(user)
        try:
            await session.commit()
        except IntegrityError:
            raise HTTPException(409, "email already registered")
    return TokenResponse(access_token=create_access_token(settings, user.id), user_id=user.id, email=user.email)


@router.post("/login", response_model=TokenResponse)
async def login(body: Credentials, request: Request):
    settings = request.app.state.settings
    async with request.app.state.session_factory() as session:
        user = await session.scalar(select(User).where(User.email == body.email))
    if user is None or not await verify_password(body.password, user.password_hash):
        raise HTTPException(401, "wrong email or password")
    return TokenResponse(access_token=create_access_token(settings, user.id), user_id=user.id, email=user.email)
