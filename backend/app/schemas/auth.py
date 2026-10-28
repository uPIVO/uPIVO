"""Request and response models for registration and login."""

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.auth.email import normalize_email


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=128)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return normalize_email(value)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return normalize_email(value)


class RegisteredUser(BaseModel):
    id: int
    email: str
    created_at: datetime


class LoginResponse(BaseModel):
    token: str
    token_type: str = "bearer"
    user_id: int
    email: str
