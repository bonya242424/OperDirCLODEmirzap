"""Задачи Битрикс24 за период и обновления по ним (только владелец)."""

from datetime import date

import secrets

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import bitrix_watch as bw
from ..bitrix import BitrixError, call
from ..db import get_db
from ..deps import current_user
from ..models import User
from ..security import hash_password

router = APIRouter(prefix="/api/bitrix", tags=["bitrix"])


def _owner(me: User) -> None:
    if me.role != "owner":
        raise HTTPException(status_code=403, detail="Задачи Битрикс24 видит только владелец")


@router.get("/employees")
def employees(db: Session = Depends(get_db), me: User = Depends(current_user)):
    _owner(me)
    return [{"bitrix_user_id": k, "name": v} for k, v in sorted(bw.tracked_users(db).items(), key=lambda kv: kv[1])]


@router.get("/tasks")
def tasks(date_from: date, date_to: date, kind: str = "any", user_id: int | None = None, db: Session = Depends(get_db), me: User = Depends(current_user)):
    _owner(me)
    if date_to < date_from or (date_to - date_from).days > 400:
        raise HTTPException(status_code=422, detail="Период указан неверно (не больше 400 дней)")
    try:
        return bw.list_period(db, date_from, date_to, kind, user_id)
    except BitrixError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/events")
def events(days: int = 7, task_id: int | None = None, db: Session = Depends(get_db), me: User = Depends(current_user)):
    _owner(me)
    return bw.recent_events(db, days, task_id)


@router.post("/watch-now")
def watch_now(db: Session = Depends(get_db), me: User = Depends(current_user)):
    """Проверить обновления прямо сейчас. Первая проверка запоминает текущее состояние и событий не создаёт."""
    _owner(me)
    try:
        return bw.watch_cycle(db)
    except BitrixError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


PENDING_DOMAIN = "pending.invalid"


class ImportIn(BaseModel):
    ids: list[int]


@router.post("/import-users")
def import_users(body: ImportIn, db: Session = Depends(get_db), me: User = Depends(current_user)):
    """Добавляет сотрудников по id из Битрикс24: ФИО, должность и почта (если Битрикс24 её отдаёт) берутся оттуда.
    Без почты создаётся временный адрес и недоступный пароль: войти нельзя, пока владелец не задаст почту и пароль в «Сотрудниках»."""
    _owner(me)
    ids = list(dict.fromkeys(i for i in body.ids if i > 0))[:100]
    if not ids:
        raise HTTPException(status_code=422, detail="Укажите хотя бы один id")
    out = []
    for bid in ids:
        existing = db.scalar(select(User).where(User.bitrix_user_id == bid))
        if existing is not None:
            out.append({"bitrix_user_id": bid, "status": "exists", "name": existing.full_name or existing.email})
            continue
        try:
            data = call("user.get", {"FILTER[ID]": bid})
        except BitrixError as exc:
            raise HTTPException(status_code=422, detail=f"{exc}. Для списка сотрудников у вебхука нужно право «Пользователи (user)»") from exc
        found = data.get("result") or []
        if not found:
            out.append({"bitrix_user_id": bid, "status": "not_found", "name": ""})
            continue
        p = found[0]
        name = " ".join(x for x in (str(p.get("LAST_NAME") or ""), str(p.get("NAME") or ""), str(p.get("SECOND_NAME") or "")) if x).strip()
        email = str(p.get("EMAIL") or "").strip().lower()
        has_email = bool(email) and "@" in email and db.scalar(select(User.id).where(User.email == email)) is None
        user = User(
            email=email if has_email else f"b24-{bid}@{PENDING_DOMAIN}",
            password_hash=hash_password(secrets.token_urlsafe(32)),  # пароль неизвестен никому: владелец задаст свой
            full_name=name[:255] or f"Сотрудник Б24 #{bid}",
            position=str(p.get("WORK_POSITION") or "")[:255],
            role="employee",
            bitrix_user_id=bid,
        )
        db.add(user)
        db.commit()
        out.append({"bitrix_user_id": bid, "status": "created", "name": user.full_name, "email": user.email, "needs_email": not has_email})
    return out
