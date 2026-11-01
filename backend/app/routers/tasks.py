from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import bitrix
from ..db import get_db
from ..deps import current_user
from ..models import BitrixTask, User, utcnow
from ..schemas import _utc

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


class TaskOut(BaseModel):
    id: int
    title: str
    responsible: str
    status: int
    deadline: datetime | None
    overdue: bool
    days_left: int | None

    _fix = field_validator("deadline")(_utc)


class PersonSummary(BaseModel):
    name: str
    open: int
    overdue: int
    due_soon: int


class TasksOut(BaseModel):
    total_open: int
    overdue: int
    due_today: int
    due_soon: int  # в ближайшие 3 дня, не считая просроченных
    no_deadline: int
    people: list[PersonSummary]
    items: list[TaskOut]


@router.get("", response_model=TasksOut)
def list_tasks(db: Session = Depends(get_db), _: User = Depends(current_user)):
    now = utcnow()
    names = {
        u.bitrix_user_id: (u.full_name or u.email)
        for u in db.scalars(select(User).where(User.bitrix_user_id.is_not(None)))
    }
    rows = db.scalars(select(BitrixTask).where(BitrixTask.is_open.is_(True))).all()

    def key(t: BitrixTask):
        return (t.deadline is None, t.deadline or now)

    items: list[TaskOut] = []
    people: dict[str, PersonSummary] = {}
    overdue = today = soon = no_deadline = 0
    for t in sorted(rows, key=key):
        deadline = _utc(t.deadline)
        who = names.get(t.responsible_id, f"id {t.responsible_id}")
        person = people.setdefault(who, PersonSummary(name=who, open=0, overdue=0, due_soon=0))
        person.open += 1
        is_overdue = deadline is not None and deadline < now
        days_left = None if deadline is None else (deadline.date() - now.date()).days
        if deadline is None:
            no_deadline += 1
        elif is_overdue:
            overdue += 1
            person.overdue += 1
        else:
            if days_left == 0:
                today += 1
            if deadline <= now + timedelta(days=3):
                soon += 1
                person.due_soon += 1
        items.append(
            TaskOut(
                id=t.id, title=t.title, responsible=who, status=t.status,
                deadline=deadline, overdue=is_overdue, days_left=days_left,
            )
        )
    return TasksOut(
        total_open=len(rows), overdue=overdue, due_today=today, due_soon=soon, no_deadline=no_deadline,
        people=sorted(people.values(), key=lambda p: (-p.overdue, p.name)), items=items,
    )


@router.post("/sync")
def sync_now(db: Session = Depends(get_db), _: User = Depends(current_user)):
    try:
        return {"open_tasks": bitrix.sync_tasks(db)}
    except bitrix.BitrixError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
