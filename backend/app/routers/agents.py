import json
import re
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agents.executors import EXECUTORS
from ..agents.service import compute_next, execute_run, start_run
from ..db import get_db
from ..deps import current_user
from ..models import Agent, AgentRun, User, utcnow
from ..schemas import _utc

router = APIRouter(prefix="/api/agents", tags=["agents"])
MIN_INTERVAL = 5


class AgentIn(BaseModel):
    name: str
    kind: str
    instruction: str = ""
    schedule_kind: Literal["manual", "interval", "daily"] = "manual"
    interval_minutes: int | None = None
    daily_time: str | None = None
    enabled: bool = True
    level: Literal["manager", "analyst", "ops"] = "ops"
    parent_id: int | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) > 200:
            raise ValueError("Название агента: от 1 до 200 символов")
        return v

    @field_validator("instruction")
    @classmethod
    def _instr(cls, v: str) -> str:
        if len(v) > 4000:
            raise ValueError("Поручение длиннее 4000 символов")
        return v.strip()

    @model_validator(mode="after")
    def _check(self):
        spec = EXECUTORS.get(self.kind)
        if spec is None:
            raise ValueError("Неизвестный тип агента")
        if spec["needs_instruction"] and not self.instruction:
            raise ValueError("Напишите поручение для агента")
        if self.schedule_kind == "interval":
            if not self.interval_minutes or self.interval_minutes < MIN_INTERVAL or self.interval_minutes > 60 * 24 * 31:
                raise ValueError(f"Интервал: от {MIN_INTERVAL} минут до 31 дня")
            self.daily_time = None
        elif self.schedule_kind == "daily":
            if not self.daily_time or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", self.daily_time):
                raise ValueError("Время запуска в формате 09:30")
            self.interval_minutes = None
        else:
            self.interval_minutes = self.daily_time = None
        return self


class RunOut(BaseModel):
    id: int
    trigger: str
    status: str
    summary: str
    log: list[dict]
    started_at: datetime
    finished_at: datetime | None

    _fix = field_validator("started_at", "finished_at")(_utc)


class AgentOut(BaseModel):
    id: int
    owner_id: int
    owner: str
    name: str
    kind: str
    kind_title: str
    instruction: str
    schedule_kind: str
    interval_minutes: int | None
    daily_time: str | None
    enabled: bool
    level: str
    parent_id: int | None
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_run: RunOut | None
    editable: bool

    _fix = field_validator("next_run_at", "last_run_at")(_utc)


def _run_out(r: AgentRun) -> RunOut:
    return RunOut(
        id=r.id, trigger=r.trigger, status=r.status, summary=r.summary, log=json.loads(r.log or "[]"),
        started_at=r.started_at, finished_at=r.finished_at,
    )


def _out(db: Session, a: Agent, me: User) -> AgentOut:
    owner = db.get(User, a.owner_id)
    last = db.scalar(select(AgentRun).where(AgentRun.agent_id == a.id).order_by(AgentRun.id.desc()).limit(1))
    return AgentOut(
        id=a.id, owner_id=a.owner_id, owner=(owner.full_name or owner.email) if owner else "", name=a.name, kind=a.kind,
        kind_title=EXECUTORS[a.kind]["title"] if a.kind in EXECUTORS else a.kind, instruction=a.instruction,
        schedule_kind=a.schedule_kind, interval_minutes=a.interval_minutes, daily_time=a.daily_time, enabled=a.enabled, level=a.level, parent_id=a.parent_id,
        next_run_at=a.next_run_at, last_run_at=a.last_run_at, last_run=_run_out(last) if last else None,
        editable=me.role == "owner" or a.owner_id == me.id,
    )


def _get(db: Session, agent_id: int, me: User) -> Agent:
    """Свои агенты видны и доступны своему владельцу; все агенты видны и доступны владельцу компании."""
    a = db.get(Agent, agent_id)
    if a is None or (a.owner_id != me.id and me.role != "owner"):
        raise HTTPException(status_code=404, detail="Агент не найден")
    return a


PARENT_LEVEL = {"ops": "analyst", "analyst": "manager"}


def _check_parent(db: Session, body: AgentIn, me: User, self_id: int | None = None) -> None:
    if body.level == "manager":
        body.parent_id = None
        return
    if body.parent_id is None:
        return
    parent = db.get(Agent, body.parent_id)
    if parent is None or (parent.owner_id != me.id and me.role != "owner"):
        raise HTTPException(status_code=422, detail="Руководитель не найден")
    if parent.id == self_id or parent.level != PARENT_LEVEL[body.level]:
        names = {"analyst": "аналитику", "manager": "руководителю"}
        raise HTTPException(status_code=422, detail=f"Агент этого уровня подчиняется только {names[PARENT_LEVEL[body.level]]}")


@router.get("/kinds")
def kinds(_: User = Depends(current_user)):
    return [{"key": k, "title": v["title"], "description": v["description"], "needs_instruction": v["needs_instruction"]} for k, v in EXECUTORS.items()]


@router.get("", response_model=list[AgentOut])
def list_agents(db: Session = Depends(get_db), me: User = Depends(current_user)):
    q = select(Agent).order_by(Agent.id)
    if me.role != "owner":
        q = q.where(Agent.owner_id == me.id)
    return [_out(db, a, me) for a in db.scalars(q)]


@router.post("", response_model=AgentOut, status_code=201)
def create(body: AgentIn, db: Session = Depends(get_db), me: User = Depends(current_user)):
    _check_parent(db, body, me)
    a = Agent(owner_id=me.id, **body.model_dump())
    a.next_run_at = compute_next(a)
    db.add(a)
    db.commit()
    return _out(db, a, me)


@router.put("/{agent_id}", response_model=AgentOut)
def update(agent_id: int, body: AgentIn, db: Session = Depends(get_db), me: User = Depends(current_user)):
    a = _get(db, agent_id, me)
    _check_parent(db, body, me, a.id)
    for k, v in body.model_dump().items():
        setattr(a, k, v)
    a.next_run_at = compute_next(a)
    db.commit()
    return _out(db, a, me)


@router.delete("/{agent_id}", status_code=204)
def delete(agent_id: int, db: Session = Depends(get_db), me: User = Depends(current_user)):
    a = _get(db, agent_id, me)
    for r in db.scalars(select(AgentRun).where(AgentRun.agent_id == a.id)):
        db.delete(r)
    for child in db.scalars(select(Agent).where(Agent.parent_id == a.id)):
        child.parent_id = None  # подчинённые остаются без руководителя
    db.delete(a)
    db.commit()


@router.post("/{agent_id}/run", response_model=RunOut, status_code=202)
def run_now(agent_id: int, tasks: BackgroundTasks, db: Session = Depends(get_db), me: User = Depends(current_user)):
    a = _get(db, agent_id, me)
    busy = db.scalar(select(AgentRun.id).where(AgentRun.agent_id == a.id, AgentRun.status.in_(("queued", "running"))))
    if busy:
        raise HTTPException(status_code=409, detail="Агент уже работает: дождитесь конца запуска")
    run = start_run(db, a, "manual")
    tasks.add_task(execute_run, run.id)
    return _run_out(run)


@router.post("/runs/{run_id}/stop", response_model=RunOut)
def stop(run_id: int, db: Session = Depends(get_db), me: User = Depends(current_user)):
    run = db.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Запуск не найден")
    _get(db, run.agent_id, me)
    if run.status == "queued":
        run.status, run.summary, run.finished_at = "stopped", "Остановлено до начала работы", utcnow()
        db.commit()
    elif run.status == "running":
        raise HTTPException(status_code=409, detail="Запуск уже идёт и закончится сам: он короткий")
    return _run_out(run)


@router.get("/{agent_id}/runs", response_model=list[RunOut])
def runs(agent_id: int, limit: int = 20, db: Session = Depends(get_db), me: User = Depends(current_user)):
    _get(db, agent_id, me)
    rows = db.scalars(select(AgentRun).where(AgentRun.agent_id == agent_id).order_by(AgentRun.id.desc()).limit(max(1, min(limit, 100))))
    return [_run_out(r) for r in rows]
