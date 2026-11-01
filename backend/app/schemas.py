from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator


def _utc(value: datetime | None) -> datetime | None:
    # SQLite отдаёт даты без часового пояса; в базе всё хранится в UTC.
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _check_password(value: str) -> str:
    if len(value) < 8:
        raise ValueError("Пароль должен быть не короче 8 символов")
    if len(value.encode("utf-8")) > 72:
        raise ValueError("Пароль слишком длинный (не больше 72 байт)")
    return value


def _check_email(value: str) -> str:
    value = value.strip().lower()
    if "@" not in value or value.startswith("@") or value.endswith("@"):
        raise ValueError("Некорректный email")
    return value


class LoginIn(BaseModel):
    email: str
    password: str

    _email = field_validator("email")(_check_email)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    full_name: str
    position: str
    department: str
    role: str
    can_apply_changes: bool
    bitrix_user_id: int | None
    is_active: bool


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class UserCreate(BaseModel):
    email: str
    password: str
    full_name: str = ""
    position: str = ""
    department: str = ""
    role: Literal["owner", "employee"] = "employee"
    can_apply_changes: bool = False
    bitrix_user_id: int | None = None

    _email = field_validator("email")(_check_email)
    _password = field_validator("password")(_check_password)


class UserUpdate(BaseModel):
    full_name: str | None = None
    position: str | None = None
    department: str | None = None
    password: str | None = None
    role: Literal["owner", "employee"] | None = None
    can_apply_changes: bool | None = None
    bitrix_user_id: int | None = None
    is_active: bool | None = None

    @field_validator("password")
    @classmethod
    def _password(cls, value: str | None) -> str | None:
        return None if value is None else _check_password(value)


class IntegrationOut(BaseModel):
    key: str
    name: str
    description: str
    status: str
    message: str
    has_sync: bool
    last_check_at: datetime | None
    last_sync_at: datetime | None

    _fix_dates = field_validator("last_check_at", "last_sync_at")(_utc)


class IntegrationsOut(BaseModel):
    check_interval_minutes: int
    items: list[IntegrationOut]


class IntegrationCheckOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    checked_at: datetime
    status: str
    message: str

    _fix_dates = field_validator("checked_at")(_utc)


class ChatIn(BaseModel):
    message: str

    @field_validator("message")
    @classmethod
    def _not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Пустое сообщение")
        if len(value) > 8000:
            raise ValueError("Сообщение слишком длинное (не больше 8000 символов)")
        return value


class ChatMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    role: str
    content: str
    created_at: datetime

    _fix_dates = field_validator("created_at")(_utc)


class ChatOut(BaseModel):
    reply: ChatMessageOut
