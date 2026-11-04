"""Задачи Битрикс24 за период и слежение за обновлениями.

Период берётся из Битрикс24 по запросу (любые статусы, в том числе закрытые). Раз в полчаса проверка смотрит, что изменилось у задач
выбранных сотрудников (статус, срок, исполнитель, название, новые комментарии), и копит события. Цифры и тексты берутся из Битрикс24, модель их не придумывает.
Код написан по документации REST API (tasks.task.list, task.commentitem.getlist); первый прогон на живом портале проверяйте вместе с владельцем.
"""

import logging
import time
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .bitrix import BitrixError, call, webhook_url
from .config import settings
from .db import SessionLocal
from .models import AppSetting, BitrixTaskEvent, BitrixTaskState, User, utcnow

log = logging.getLogger(__name__)

STATUS = {1: "Новая", 2: "Ждёт выполнения", 3: "Выполняется", 4: "Ждёт контроля", 5: "Завершена", 6: "Отложена", 7: "Отклонена"}
SELECT = ["ID", "TITLE", "STATUS", "RESPONSIBLE_ID", "CREATED_BY", "CREATED_DATE", "CHANGED_DATE", "CLOSED_DATE", "DEADLINE", "GROUP_ID", "PRIORITY", "DESCRIPTION", "PARENT_ID", "COMMENTS_COUNT"]
PAGE_LIMIT = 40  # страниц по 50 задач на один запрос: до 2000
DATE_FIELDS = {"created": "CREATED_DATE", "closed": "CLOSED_DATE", "deadline": "DEADLINE", "changed": "CHANGED_DATE"}
_CACHE: dict[tuple, tuple[float, list[dict]]] = {}


def _pick(raw: dict, *names: str):
    for n in names:
        if n in raw and raw[n] not in (None, ""):
            return raw[n]
    return None


def _dt(value) -> datetime | None:
    if not value:
        return None
    try:
        d = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)  # везде храним и сравниваем по UTC


def _fmt(d: datetime | None) -> str:
    """Дата по Москве для текстов событий."""
    return _aware(d).astimezone(timezone(timedelta(hours=3))).strftime("%d.%m.%Y") if d else "нет"


def _aware(d: datetime | None) -> datetime | None:
    """SQLite отдаёт время без пояса, Postgres с поясом: приводим к одному виду перед сравнением."""
    return d if d is None or d.tzinfo else d.replace(tzinfo=timezone.utc)


def _norm(raw: dict) -> dict:
    status = int(_pick(raw, "status", "STATUS") or 0)
    return {
        "id": int(_pick(raw, "id", "ID") or 0),
        "title": str(_pick(raw, "title", "TITLE") or "")[:500],
        "status": status,
        "status_name": STATUS.get(status, f"Статус {status}"),
        "responsible_id": int(_pick(raw, "responsibleId", "RESPONSIBLE_ID") or 0),
        "created_by": int(_pick(raw, "createdBy", "CREATED_BY") or 0),
        "created": _dt(_pick(raw, "createdDate", "CREATED_DATE")),
        "changed": _dt(_pick(raw, "changedDate", "CHANGED_DATE")),
        "closed": _dt(_pick(raw, "closedDate", "CLOSED_DATE")),
        "deadline": _dt(_pick(raw, "deadline", "DEADLINE")),
        "group_id": int(_pick(raw, "groupId", "GROUP_ID") or 0),
        "parent_id": int(_pick(raw, "parentId", "PARENT_ID") or 0),
        "description": str(_pick(raw, "description", "DESCRIPTION") or "")[:5000],
        "comments_count": int(_pick(raw, "commentsCount", "COMMENTS_COUNT") or 0),
    }


def fetch(filter_: dict) -> list[dict]:
    """Все страницы tasks.task.list по фильтру."""
    out: list[dict] = []
    start: int | None = 0
    for _ in range(PAGE_LIMIT):
        if start is None:
            break
        params: dict = {"select[]": SELECT, "start": start, "order[ID]": "asc"}
        params.update({f"filter[{k}]": v for k, v in filter_.items()})
        data = call("tasks.task.list", params)
        try:
            out += [_norm(t) for t in data["result"]["tasks"]]
        except (KeyError, TypeError):
            raise BitrixError("Неожиданный ответ Битрикс24: проверьте права вебхука на задачи") from None
        nxt = data.get("next")
        start = int(nxt) if nxt is not None else None
    else:
        raise BitrixError("Слишком много задач за период: сузьте период или сотрудников")
    return out


def tracked_users(db: Session) -> dict[int, str]:
    """Сотрудники системы с id в Битрикс24: {id в Б24: имя}."""
    return {u.bitrix_user_id: (u.full_name or u.email) for u in db.scalars(select(User).where(User.is_active.is_(True), User.bitrix_user_id.is_not(None))) if u.bitrix_user_id is not None}


def list_period(db: Session, date_from: date, date_to: date, kind: str = "any", user_id: int | None = None) -> dict:
    """Задачи выбранных сотрудников за период. kind: created, closed, deadline, changed или any (создана, закрыта или срок в периоде)."""
    users = tracked_users(db)
    ids = [user_id] if user_id else sorted(users)
    if not ids:
        raise BitrixError("Не выбрано ни одного сотрудника с id в Битрикс24: укажите его в карточке сотрудника")
    if kind not in (*DATE_FIELDS, "any"):
        raise BitrixError("Вид даты: created, closed, deadline, changed или any")
    key = (date_from, date_to, kind, tuple(ids))
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < 180:
        tasks = hit[1]
    else:
        end = datetime.combine(date_to, datetime.max.time()).strftime("%Y-%m-%d %H:%M:%S")
        begin = datetime.combine(date_from, datetime.min.time()).strftime("%Y-%m-%d %H:%M:%S")
        fields = list(DATE_FIELDS.values())[:3] if kind == "any" else [DATE_FIELDS[kind]]
        by_id: dict[int, dict] = {}
        for uid in ids:
            for f in fields:
                for t in fetch({"RESPONSIBLE_ID": uid, f">={f}": begin, f"<={f}": end}):
                    by_id[t["id"]] = t
        tasks = sorted(by_id.values(), key=lambda t: (t["created"] or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
        _CACHE[key] = (time.monotonic(), tasks)
    out = []
    for t in tasks:
        out.append({**{k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in t.items() if k != "description"}, "responsible": users.get(t["responsible_id"], f"Б24 #{t['responsible_id']}")})
    by_status: dict[str, int] = {}
    for t in out:
        by_status[t["status_name"]] = by_status.get(t["status_name"], 0) + 1
    return {"count": len(out), "by_status": by_status, "tasks": out}


def _comments(task_id: int, after: int) -> list[dict]:
    data = call("task.commentitem.getlist", {"TASKID": task_id})
    res = data.get("result") or []
    out = []
    for c in res if isinstance(res, list) else []:
        cid = int(_pick(c, "ID", "id") or 0)
        if cid > after:
            out.append({"id": cid, "author": str(_pick(c, "AUTHOR_NAME", "authorName") or _pick(c, "AUTHOR_ID", "authorId") or ""), "text": str(_pick(c, "POST_MESSAGE", "postMessage") or "")})
    return sorted(out, key=lambda c: c["id"])


def _event(db: Session, t: dict, kind: str, text: str) -> None:
    db.add(BitrixTaskEvent(task_id=t["id"], kind=kind, title=t["title"], text=text[:1500], responsible_id=t["responsible_id"]))


def watch_cycle(db: Session) -> dict:
    """Один проход: берём задачи выбранных сотрудников, изменённые с прошлой проверки, и записываем, что поменялось."""
    users = tracked_users(db)
    if not users:
        raise BitrixError("Не выбрано ни одного сотрудника с id в Битрикс24")
    mark = db.get(AppSetting, "bitrix_watch_at")
    baseline = db.scalar(select(BitrixTaskState.task_id).limit(1)) is None
    since = (_dt(mark.value) if mark and mark.value else None) or (utcnow() - timedelta(days=30))
    since = since - timedelta(minutes=15)  # запас на разницу часов
    begin = since.astimezone(timezone(timedelta(hours=3))).strftime("%Y-%m-%d %H:%M:%S")
    started = utcnow()
    new_events, fetched, comment_calls = 0, 0, 0
    for uid in sorted(users):
        for t in fetch({"RESPONSIBLE_ID": uid, ">=CHANGED_DATE": begin}):
            fetched += 1
            st = db.get(BitrixTaskState, t["id"])
            if st is None:
                db.add(BitrixTaskState(task_id=t["id"], title=t["title"], status=t["status"], responsible_id=t["responsible_id"], deadline=t["deadline"], changed_at=t["changed"]))
                if not baseline:
                    _event(db, t, "new", f"Новая задача, исполнитель {users.get(t['responsible_id'], t['responsible_id'])}, срок {_fmt(t['deadline'])}" if t["deadline"] else f"Новая задача, исполнитель {users.get(t['responsible_id'], t['responsible_id'])}")
                    new_events += 1
                continue
            if baseline:
                continue
            if t["status"] != st.status:
                _event(db, t, "status", f"Статус: {STATUS.get(st.status, st.status)} → {STATUS.get(t['status'], t['status'])}")
                new_events += 1
            if _aware(t["deadline"]) != _aware(st.deadline):
                _event(db, t, "deadline", f"Срок: {_fmt(st.deadline)} → {_fmt(t['deadline'])}")
                new_events += 1
            if t["responsible_id"] != st.responsible_id:
                _event(db, t, "responsible", f"Исполнитель: {users.get(st.responsible_id, st.responsible_id)} → {users.get(t['responsible_id'], t['responsible_id'])}")
                new_events += 1
            if t["title"] != st.title:
                _event(db, t, "title", f"Название: «{st.title[:100]}» → «{t['title'][:100]}»")
                new_events += 1
            if t["changed"] and st.changed_at and t["changed"] > _aware(st.changed_at) and comment_calls < 40:
                comment_calls += 1
                try:
                    for c in _comments(t["id"], st.last_comment_id):
                        _event(db, t, "comment", f"Комментарий ({c['author']}): {c['text']}")
                        st.last_comment_id = max(st.last_comment_id, c["id"])
                        new_events += 1
                except BitrixError:
                    pass  # комментарии вторичны: статус и сроки уже записаны
            st.title, st.status, st.responsible_id, st.deadline, st.changed_at, st.seen_at = t["title"], t["status"], t["responsible_id"], t["deadline"], t["changed"], utcnow()
    if mark is None:
        db.add(AppSetting(key="bitrix_watch_at", value=started.isoformat()))
    else:
        mark.value = started.isoformat()
    db.commit()
    return {"baseline": baseline, "checked": fetched, "events": new_events}


def recent_events(db: Session, days: int = 7, task_id: int | None = None, limit: int = 200) -> list[dict]:
    since = utcnow() - timedelta(days=max(1, min(days, 365)))
    q = select(BitrixTaskEvent).where(BitrixTaskEvent.at >= since).order_by(BitrixTaskEvent.id.desc()).limit(limit)
    if task_id:
        q = q.where(BitrixTaskEvent.task_id == task_id)
    users = tracked_users(db)
    return [
        {"id": e.id, "task_id": e.task_id, "title": e.title, "kind": e.kind, "text": e.text, "at": e.at.isoformat(), "responsible": users.get(e.responsible_id, f"Б24 #{e.responsible_id}")}
        for e in db.scalars(q)
    ]


def run_scheduled_watch() -> None:
    if not webhook_url():
        return
    with SessionLocal() as db:
        try:
            res = watch_cycle(db)
            if res["events"]:
                log.info("Битрикс24: %s обновлений по задачам", res["events"])
        except BitrixError as exc:
            log.warning("Слежение за задачами Битрикс24: %s", exc)
        except Exception:  # noqa: BLE001
            log.exception("Слежение за задачами Битрикс24 упало")
