import json
import re
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agents.presets import PRESETS
from ..agents.service import compute_next, execute_run, start_run
from ..agents.tools import TOOLS, Ctx, ToolError, parse_tools
from ..db import get_db
from ..deps import current_user
from ..models import Agent, AgentAction, AgentRun, User, utcnow
from ..schemas import _utc

router = APIRouter(prefix="/api/agents", tags=["agents"])
MIN_INTERVAL = 5


class AgentIn(BaseModel):
    name: str
    kind: str = "custom"
    instruction: str
    tools: list[str] = []
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
        if not self.instruction:
            raise ValueError("Напишите поручение для агента")
        unknown = [t for t in self.tools if t not in TOOLS]
        if unknown:
            raise ValueError(f"Неизвестный инструмент: {unknown[0]}")
        self.tools = list(dict.fromkeys(self.tools))
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
    tools: list[str]
    pending_actions: int
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
        kind_title=next((p["title"] for p in PRESETS if p["key"] == a.kind), "Свой агент"), instruction=a.instruction,
        tools=parse_tools(a.tools),
        pending_actions=len(db.scalars(select(AgentAction.id).where(AgentAction.agent_id == a.id, AgentAction.status == "pending")).all()),
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


@router.get("/presets")
def presets(_: User = Depends(current_user)):
    return PRESETS


@router.get("/tools")
def tools(_: User = Depends(current_user)):
    return [{"key": t.name, "title": t.title, "description": t.description, "kind": t.kind} for t in TOOLS.values()]


@router.get("", response_model=list[AgentOut])
def list_agents(db: Session = Depends(get_db), me: User = Depends(current_user)):
    q = select(Agent).order_by(Agent.id)
    if me.role != "owner":
        q = q.where(Agent.owner_id == me.id)
    return [_out(db, a, me) for a in db.scalars(q)]


@router.post("", response_model=AgentOut, status_code=201)
def create(body: AgentIn, db: Session = Depends(get_db), me: User = Depends(current_user)):
    _check_parent(db, body, me)
    a = Agent(owner_id=me.id, **{**body.model_dump(), "tools": json.dumps(body.tools)})
    a.next_run_at = compute_next(a)
    db.add(a)
    db.commit()
    return _out(db, a, me)


@router.put("/{agent_id}", response_model=AgentOut)
def update(agent_id: int, body: AgentIn, db: Session = Depends(get_db), me: User = Depends(current_user)):
    a = _get(db, agent_id, me)
    _check_parent(db, body, me, a.id)
    for k, v in {**body.model_dump(), "tools": json.dumps(body.tools)}.items():
        setattr(a, k, v)
    a.next_run_at = compute_next(a)
    db.commit()
    return _out(db, a, me)


@router.delete("/{agent_id}", status_code=204)
def delete(agent_id: int, db: Session = Depends(get_db), me: User = Depends(current_user)):
    a = _get(db, agent_id, me)
    for x in db.scalars(select(AgentAction).where(AgentAction.agent_id == a.id)):
        db.delete(x)
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


# ---- Предложения агентов: выполняет человек ----


class ActionOut(BaseModel):
    id: int
    agent_id: int
    agent: str
    title: str
    tool: str
    args: dict
    status: str
    result: str
    created_at: datetime

    _fix = field_validator("created_at")(_utc)


def _action_out(db: Session, x: AgentAction) -> ActionOut:
    agent = db.get(Agent, x.agent_id)
    return ActionOut(id=x.id, agent_id=x.agent_id, agent=agent.name if agent else "", title=x.title, tool=x.tool,
                     args=json.loads(x.args or "{}"), status=x.status, result=x.result, created_at=x.created_at)


def _visible_action(db: Session, action_id: int, me: User) -> AgentAction:
    x = db.get(AgentAction, action_id)
    if x is None:
        raise HTTPException(status_code=404, detail="Предложение не найдено")
    _get(db, x.agent_id, me)
    return x


@router.get("/actions", response_model=list[ActionOut])
def actions(status: str = "pending", db: Session = Depends(get_db), me: User = Depends(current_user)):
    q = select(AgentAction).where(AgentAction.status == status).order_by(AgentAction.id.desc()).limit(100)
    if me.role != "owner":
        q = q.join(Agent, Agent.id == AgentAction.agent_id).where(Agent.owner_id == me.id)
    return [_action_out(db, x) for x in db.scalars(q)]


@router.post("/actions/{action_id}/approve", response_model=ActionOut)
def approve(action_id: int, db: Session = Depends(get_db), me: User = Depends(current_user)):
    x = _visible_action(db, action_id, me)
    if x.status != "pending":
        raise HTTPException(status_code=409, detail="Предложение уже обработано")
    tool = TOOLS.get(x.tool)
    try:
        if tool is None or tool.execute is None:
            raise ToolError("Это действие больше не поддерживается")
        x.result, x.status = tool.execute(db, me, json.loads(x.args or "{}")), "done"
    except ToolError as exc:
        x.result, x.status = str(exc), "failed"
    x.decided_by, x.decided_at = me.id, utcnow()
    db.commit()
    return _action_out(db, x)


@router.post("/actions/{action_id}/reject", response_model=ActionOut)
def reject(action_id: int, db: Session = Depends(get_db), me: User = Depends(current_user)):
    x = _visible_action(db, action_id, me)
    if x.status != "pending":
        raise HTTPException(status_code=409, detail="Предложение уже обработано")
    x.status, x.decided_by, x.decided_at = "rejected", me.id, utcnow()
    db.commit()
    return _action_out(db, x)
