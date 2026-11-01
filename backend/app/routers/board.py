from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user
from ..models import BitrixTask, Task, User, utcnow
from ..schemas import _utc

router = APIRouter(prefix="/api/board", tags=["board"])

Status = Literal["new", "in_progress", "review", "done"]
Priority = Literal["low", "normal", "high"]
COLUMNS: list[tuple[str, str]] = [
    ("new", "Новые"),
    ("in_progress", "В работе"),
    ("review", "На проверке"),
    ("done", "Готово"),
]
# Статусы Битрикс24: 1,2 ждёт; 3 в работе; 4 ждёт проверки; 6 отложена
BITRIX_COLUMN = {1: "new", 2: "new", 3: "in_progress", 4: "review", 6: "new"}
DONE_VISIBLE_DAYS = 14
STEP = 1000.0


class TaskIn(BaseModel):
    title: str
    description: str = ""
    status: Status = "new"
    priority: Priority = "normal"
    assignee_id: int | None = None
    deadline: datetime | None = None

    @field_validator("title")
    @classmethod
    def _title(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) > 300:
            raise ValueError("Название задачи: от 1 до 300 символов")
        return v


class TaskPatch(BaseModel):
    title: str | None = None
    description: str | None = None
    priority: Priority | None = None
    assignee_id: int | None = None
    deadline: datetime | None = None
    clear_assignee: bool = False
    clear_deadline: bool = False


class MoveIn(BaseModel):
    status: Status
    index: int = 0  # место в колонке, считая с нуля


class CardOut(BaseModel):
    id: str  # "l-12" своя задача, "b-345" из Битрикс24
    source: Literal["local", "bitrix"]
    title: str
    description: str
    status: str
    priority: str
    assignee_id: int | None
    assignee: str
    deadline: datetime | None
    overdue: bool
    editable: bool

    _fix = field_validator("deadline")(_utc)


class ColumnOut(BaseModel):
    key: str
    title: str
    cards: list[CardOut]


class BoardOut(BaseModel):
    columns: list[ColumnOut]


def _names(db: Session) -> tuple[dict[int, str], dict[int, str]]:
    users = list(db.scalars(select(User)))
    by_id = {u.id: (u.full_name or u.email) for u in users}
    by_bitrix = {u.bitrix_user_id: (u.full_name or u.email) for u in users if u.bitrix_user_id is not None}
    return by_id, by_bitrix


def _card(t: Task, names: dict[int, str]) -> CardOut:
    deadline = _utc(t.deadline)
    return CardOut(
        id=f"l-{t.id}", source="local", title=t.title, description=t.description, status=t.status,
        priority=t.priority, assignee_id=t.assignee_id, assignee=names.get(t.assignee_id, "") if t.assignee_id else "",
        deadline=deadline, overdue=bool(deadline and t.status != "done" and deadline < utcnow()), editable=True,
    )


@router.get("", response_model=BoardOut)
def board(db: Session = Depends(get_db), _: User = Depends(current_user)):
    names, bitrix_names = _names(db)
    now = utcnow()
    cols: dict[str, list[CardOut]] = {k: [] for k, _t in COLUMNS}
    local = db.scalars(select(Task).order_by(Task.position, Task.id)).all()
    for t in local:
        if t.status == "done" and t.completed_at and _utc(t.completed_at) < now - timedelta(days=DONE_VISIBLE_DAYS):
            continue
        cols[t.status].append(_card(t, names))
    for b in db.scalars(select(BitrixTask).where(BitrixTask.is_open.is_(True)).order_by(BitrixTask.deadline.is_(None), BitrixTask.deadline)):
        deadline = _utc(b.deadline)
        cols[BITRIX_COLUMN.get(b.status, "new")].append(
            CardOut(
                id=f"b-{b.id}", source="bitrix", title=b.title, description="", status=BITRIX_COLUMN.get(b.status, "new"),
                priority="normal", assignee_id=None, assignee=bitrix_names.get(b.responsible_id, f"id {b.responsible_id}"),
                deadline=deadline, overdue=bool(deadline and deadline < now), editable=False,
            )
        )
    return BoardOut(columns=[ColumnOut(key=k, title=title, cards=cols[k]) for k, title in COLUMNS])


def _task(db: Session, task_id: int) -> Task:
    t = db.get(Task, task_id)
    if t is None:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    return t


def _check_assignee(db: Session, user_id: int | None) -> None:
    if user_id is not None and db.get(User, user_id) is None:
        raise HTTPException(status_code=422, detail="Исполнитель не найден")


def _renumber(db: Session, status: str, moving: Task | None = None, index: int = 0) -> None:
    rows = [t for t in db.scalars(select(Task).where(Task.status == status).order_by(Task.position, Task.id)) if moving is None or t.id != moving.id]
    if moving is not None:
        rows.insert(max(0, min(index, len(rows))), moving)
    for i, t in enumerate(rows):
        t.position = (i + 1) * STEP


@router.post("/tasks", response_model=CardOut, status_code=201)
def create(body: TaskIn, db: Session = Depends(get_db), me: User = Depends(current_user)):
    _check_assignee(db, body.assignee_id)
    t = Task(**body.model_dump(), created_by=me.id, position=1e12)
    if t.status == "done":
        t.completed_at = utcnow()
    db.add(t)
    db.flush()
    _renumber(db, t.status)
    db.commit()
    return _card(t, _names(db)[0])


@router.patch("/tasks/{task_id}", response_model=CardOut)
def update(task_id: int, body: TaskPatch, db: Session = Depends(get_db), _: User = Depends(current_user)):
    t = _task(db, task_id)
    data = body.model_dump(exclude_unset=True)
    if data.pop("clear_assignee", False):
        t.assignee_id = None
        data.pop("assignee_id", None)
    if data.pop("clear_deadline", False):
        t.deadline = None
        data.pop("deadline", None)
    if "title" in data:
        title = (data["title"] or "").strip()
        if not title or len(title) > 300:
            raise HTTPException(status_code=422, detail="Название задачи: от 1 до 300 символов")
        data["title"] = title
    if "assignee_id" in data:
        _check_assignee(db, data["assignee_id"])
    for k, v in data.items():
        setattr(t, k, v)
    db.commit()
    return _card(t, _names(db)[0])


@router.post("/tasks/{task_id}/move", response_model=CardOut)
def move(task_id: int, body: MoveIn, db: Session = Depends(get_db), _: User = Depends(current_user)):
    t = _task(db, task_id)
    old = t.status
    if body.status == "done" and old != "done":
        t.completed_at = utcnow()
    elif body.status != "done":
        t.completed_at = None
    t.status = body.status
    db.flush()
    _renumber(db, body.status, t, body.index)
    if old != body.status:
        _renumber(db, old)
    db.commit()
    return _card(t, _names(db)[0])


@router.delete("/tasks/{task_id}", status_code=204)
def delete(task_id: int, db: Session = Depends(get_db), me: User = Depends(current_user)):
    t = _task(db, task_id)
    if not (me.role == "owner" or t.created_by == me.id):
        raise HTTPException(status_code=403, detail="Удалить задачу может её автор или владелец")
    db.delete(t)
    db.commit()
