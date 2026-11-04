"""Задачи Битрикс24 за период и обновления по ним (только владелец)."""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import bitrix_watch as bw
from ..bitrix import BitrixError
from ..db import get_db
from ..deps import current_user
from ..models import User

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
