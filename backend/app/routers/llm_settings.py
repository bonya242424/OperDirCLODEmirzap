"""Выбор модели ИИ: Claude или GPT и версия модели. Ключи лежат в подключениях, здесь только выбор."""

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user
from ..llm import active_model, active_provider, llm_api_key, openai_api_key, set_setting, xai_api_key
from ..models import User

router = APIRouter(prefix="/api/llm", tags=["llm"])

# Подсказки для выбора; можно вписать любое другое название модели.
SUGGEST = {
    "anthropic": ["claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"],
    "openai": ["gpt-5", "gpt-5-mini", "gpt-4.1"],
    "xai": ["grok-4"],
}
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")


class LlmOut(BaseModel):
    provider: str
    models: dict[str, str]
    suggestions: dict[str, list[str]]
    has_key: dict[str, bool]


class LlmIn(BaseModel):
    provider: str
    model: str = ""


def _owner(me: User) -> None:
    if me.role != "owner":
        raise HTTPException(status_code=403, detail="Модель выбирает только владелец")


def _out() -> LlmOut:
    return LlmOut(
        provider=active_provider(),
        models={"anthropic": active_model("anthropic"), "openai": active_model("openai"), "xai": active_model("xai")},
        suggestions=SUGGEST,
        has_key={"anthropic": bool(llm_api_key()), "openai": bool(openai_api_key()), "xai": bool(xai_api_key())},
    )


@router.get("", response_model=LlmOut)
def get_llm(me: User = Depends(current_user)):
    _owner(me)
    return _out()


@router.put("", response_model=LlmOut)
def put_llm(body: LlmIn, db: Session = Depends(get_db), me: User = Depends(current_user)):
    _owner(me)
    if body.provider not in SUGGEST:
        raise HTTPException(status_code=422, detail="Выберите Claude, GPT или Grok")
    model = body.model.strip()
    if model and not _MODEL.match(model):
        raise HTTPException(status_code=422, detail="Название модели: латиница, цифры, точка, дефис")
    set_setting(db, "llm_provider", body.provider)
    if model:
        set_setting(db, f"llm_model_{body.provider}", model)
    return _out()
