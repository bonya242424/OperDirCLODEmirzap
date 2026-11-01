"""Клиент Битрикс24 (входящий вебхук) и синхронизация задач выбранных сотрудников.

Внимание: код написан по документации REST API tasks.task.list и проверен только на подставных ответах.
Первый прогон на живом портале нужно сделать вместе с владельцем: см. README, раздел «Битрикс24».
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .config import settings
from .db import SessionLocal
from .integrations import service
from .models import BitrixTask, User, utcnow

log = logging.getLogger(__name__)

STATUS_COMPLETED = 5
PAGE = 50
MAX_PAGES = 200  # защита от бесконечного цикла: до 10 000 задач на сотрудника


class BitrixError(Exception):
    """Текст безопасен для показа: в нём нет адреса вебхука."""


@dataclass
class RemoteTask:
    id: int
    title: str
    responsible_id: int
    status: int
    deadline: datetime | None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _pick(raw: dict, *names: str):
    for n in names:
        if n in raw:
            return raw[n]
    return None


def fetch_open_tasks(responsible_id: int) -> list[RemoteTask]:
    webhook = settings.bitrix24_webhook_url.strip().rstrip("/")
    if not webhook:
        raise BitrixError("Не задан BITRIX24_WEBHOOK_URL")
    tasks: list[RemoteTask] = []
    start: int | None = 0
    for _ in range(MAX_PAGES):
        if start is None:
            break
        params = {
            "filter[RESPONSIBLE_ID]": responsible_id,
            "filter[<REAL_STATUS]": STATUS_COMPLETED,
            "select[]": ["ID", "TITLE", "STATUS", "DEADLINE", "RESPONSIBLE_ID"],
            "start": start,
        }
        try:
            resp = httpx.post(f"{webhook}/tasks.task.list.json", data=params, timeout=20.0)
        except httpx.TimeoutException:
            raise BitrixError("Битрикс24 не ответил за 20 секунд") from None
        except httpx.HTTPError as exc:
            raise BitrixError(f"Ошибка сети ({type(exc).__name__})") from None
        if resp.status_code != 200:
            raise BitrixError(f"Битрикс24 ответил кодом {resp.status_code}: проверьте вебхук и его права на задачи")
        try:
            data = resp.json()
            raw_list = data["result"]["tasks"]
        except (ValueError, KeyError, TypeError):
            raise BitrixError("Неожиданный ответ Битрикс24: проверьте права вебхука на задачи") from None
        for raw in raw_list:
            status = int(_pick(raw, "status", "STATUS") or 0)
            if status == STATUS_COMPLETED:
                continue
            tasks.append(
                RemoteTask(
                    id=int(_pick(raw, "id", "ID")),
                    title=str(_pick(raw, "title", "TITLE") or "")[:500],
                    responsible_id=int(_pick(raw, "responsibleId", "RESPONSIBLE_ID") or responsible_id),
                    status=status,
                    deadline=_parse_dt(_pick(raw, "deadline", "DEADLINE")),
                )
            )
        nxt = data.get("next")
        start = int(nxt) if nxt is not None else None
    else:
        raise BitrixError("Слишком много страниц задач: остановили синхронизацию")
    return tasks


def sync_tasks(db: Session) -> int:
    """Обновляет открытые задачи выбранных сотрудников. Возвращает число открытых задач.

    Задачи, которых больше нет в выдаче (закрыты или удалены), помечаются закрытыми.
    Если для хотя бы одного сотрудника запрос упал, ничего не меняем: не должно быть «ложных закрытий».
    """
    users = list(db.scalars(select(User).where(User.is_active.is_(True), User.bitrix_user_id.is_not(None))))
    ids = sorted({u.bitrix_user_id for u in users if u.bitrix_user_id is not None})
    if not ids:
        raise BitrixError("Не выбрано ни одного сотрудника с id в Битрикс24")

    fetched: dict[int, RemoteTask] = {}
    for bitrix_id in ids:
        for task in fetch_open_tasks(bitrix_id):
            fetched[task.id] = task

    now = utcnow()
    existing = {t.id: t for t in db.scalars(select(BitrixTask).where(BitrixTask.responsible_id.in_(ids)))}
    for task_id, task in fetched.items():
        row = existing.get(task_id)
        if row is None:
            row = BitrixTask(id=task_id, title=task.title, responsible_id=task.responsible_id)
            db.add(row)
        row.title, row.responsible_id, row.status = task.title, task.responsible_id, task.status
        row.deadline, row.is_open, row.synced_at = task.deadline, True, now
    gone = [i for i, row in existing.items() if i not in fetched and row.is_open]
    if gone:
        db.execute(update(BitrixTask).where(BitrixTask.id.in_(gone)).values(is_open=False, synced_at=now))
    db.commit()
    service.record_sync(db, "bitrix24", now)
    return len(fetched)


def run_scheduled_sync() -> None:
    if not settings.bitrix24_webhook_url.strip():
        return
    with SessionLocal() as db:
        try:
            sync_tasks(db)
        except BitrixError as exc:
            log.warning("Синхронизация Битрикс24: %s", exc)
        except Exception:  # noqa: BLE001
            log.exception("Синхронизация Битрикс24 упала")
