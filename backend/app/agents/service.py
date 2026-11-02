import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import Agent, AgentRun, utcnow
from .executors import EXECUTORS, Ctx, ExecutorError

log = logging.getLogger(__name__)
TZ = ZoneInfo("Europe/Moscow")
MAX_LOG = 200
MAX_SUMMARY = 8000


def compute_next(agent: Agent, now: datetime | None = None) -> datetime | None:
    now = now or utcnow()
    if not agent.enabled or agent.schedule_kind == "manual":
        return None
    if agent.schedule_kind == "interval" and agent.interval_minutes:
        return now + timedelta(minutes=agent.interval_minutes)
    if agent.schedule_kind == "daily" and agent.daily_time:
        hh, mm = (int(x) for x in agent.daily_time.split(":"))
        local = now.astimezone(TZ)
        nxt = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if nxt <= local:
            nxt += timedelta(days=1)
        return nxt.astimezone(now.tzinfo)
    return None


def start_run(db: Session, agent: Agent, trigger: str) -> AgentRun:
    run = AgentRun(agent_id=agent.id, trigger=trigger, status="queued")
    db.add(run)
    db.commit()
    return run


def execute_run(run_id: int) -> None:
    """Выполняет запуск в своей сессии: вызывается из фоновой задачи и расписания."""
    with SessionLocal() as db:
        run = db.get(AgentRun, run_id)
        if run is None or run.status != "queued":
            return
        agent = db.get(Agent, run.agent_id)
        lines: list[dict] = []

        def add(text: str) -> None:
            if len(lines) < MAX_LOG:
                lines.append({"at": utcnow().isoformat(), "text": text[:500]})

        run.status = "running"
        db.commit()
        add("Запуск" + (" по расписанию" if run.trigger == "schedule" else " вручную"))
        try:
            spec = EXECUTORS.get(agent.kind)
            if spec is None:
                raise ExecutorError("Неизвестный тип агента")
            run.summary = spec["run"](Ctx(db=db, agent=agent, log=add))[:MAX_SUMMARY]
            run.status = "done"
            add("Готово")
        except ExecutorError as exc:
            run.status, run.summary = "failed", str(exc)
            add(f"Остановлено: {exc}")
        except Exception:  # noqa: BLE001
            log.exception("Агент %s упал", agent.id)
            run.status, run.summary = "failed", "Внутренняя ошибка: подробности в журнале сервера"
            add("Внутренняя ошибка")
        run.log = json.dumps(lines, ensure_ascii=False)
        run.finished_at = utcnow()
        agent.last_run_at = run.finished_at
        db.commit()


def run_due_agents() -> None:
    """Раз в минуту: запускает агентов, у которых подошло время."""
    now = utcnow()
    ids: list[int] = []
    with SessionLocal() as db:
        due = db.scalars(select(Agent).where(Agent.enabled.is_(True), Agent.next_run_at.is_not(None), Agent.next_run_at <= now))
        for agent in list(due):
            running = db.scalar(select(AgentRun.id).where(AgentRun.agent_id == agent.id, AgentRun.status.in_(("queued", "running"))))
            agent.next_run_at = compute_next(agent, now)
            if running:
                continue  # прошлый запуск не закончился: пропускаем, не копим очередь
            ids.append(start_run(db, agent, "schedule").id)
        db.commit()
    for i in ids:
        execute_run(i)


def stop_stale_runs() -> None:
    """После перезапуска сервера запуски, которые остались «в работе», помечаем остановленными."""
    with SessionLocal() as db:
        for run in db.scalars(select(AgentRun).where(AgentRun.status.in_(("queued", "running")))):
            run.status, run.summary, run.finished_at = "stopped", "Сервер перезапускался во время запуска", utcnow()
        db.commit()
