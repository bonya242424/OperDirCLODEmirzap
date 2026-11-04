"""Расширенные инструменты ассистента для владельца: задачи, показатели, агенты, папки, файлы, поставщики.

Всё обратимое: ничего не удаляется, секреты, пользователи и подключения недоступны, а возвраты не отправляются.
Необратимое остаётся за человеком.
"""

import json
import threading
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import select

from ..config import settings
from ..models import Agent, Folder, Metric, ReturnSupplier, StoredFile, Task, User, utcnow
from .tools import TOOLS, Ctx, Tool, ToolError, _obj

STATUSES = ("new", "in_progress", "review", "done")


def _author(ctx: Ctx) -> int:
    uid = ctx.user_id or (ctx.agent.owner_id if ctx.agent else None)
    if uid is None:
        raise ToolError("Не определён автор действия")
    return uid


def _assignee(ctx: Ctx, email: str | None) -> int | None:
    if not email:
        return None
    u = ctx.db.scalar(select(User).where(User.email == email.strip().lower()))
    if u is None:
        raise ToolError(f"Сотрудник с почтой {email} не найден")
    return u.id


def _deadline(v) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.combine(date.fromisoformat(str(v)), datetime.min.time(), tzinfo=timezone.utc)
    except ValueError:
        raise ToolError("Срок: дата в виде ГГГГ-ММ-ДД") from None


def create_task(ctx: Ctx, a: dict) -> str:
    title = str(a.get("title", "")).strip()
    if not title or len(title) > 300:
        raise ToolError("Нужно название задачи (до 300 символов)")
    pr = a.get("priority", "normal")
    if pr not in ("low", "normal", "high"):
        raise ToolError("priority: low, normal или high")
    t = Task(title=title, description=str(a.get("description", ""))[:4000], priority=pr, assignee_id=_assignee(ctx, a.get("assignee_email")), deadline=_deadline(a.get("deadline")), created_by=_author(ctx))
    ctx.db.add(t)
    ctx.db.commit()
    ctx.writes += 1
    return f"Задача создана: id={t.id}, «{t.title}»"


def update_task(ctx: Ctx, a: dict) -> str:
    t = ctx.db.get(Task, int(a.get("task_id", 0)))
    if t is None:
        raise ToolError("Задача не найдена")
    if "title" in a and str(a["title"]).strip():
        t.title = str(a["title"]).strip()[:300]
    if "description" in a:
        t.description = str(a["description"])[:4000]
    if "priority" in a:
        if a["priority"] not in ("low", "normal", "high"):
            raise ToolError("priority: low, normal или high")
        t.priority = a["priority"]
    if "assignee_email" in a:
        t.assignee_id = _assignee(ctx, a["assignee_email"])
    if "deadline" in a:
        t.deadline = _deadline(a["deadline"])
    if "status" in a:
        if a["status"] not in STATUSES:
            raise ToolError("status: new, in_progress, review или done")
        t.status = a["status"]
        t.completed_at = utcnow() if a["status"] == "done" else None
    ctx.db.commit()
    ctx.writes += 1
    return f"Задача id={t.id} обновлена: статус {t.status}"


def update_metric(ctx: Ctx, a: dict) -> str:
    m = ctx.db.get(Metric, int(a.get("metric_id", 0)))
    if m is None:
        raise ToolError("Показатель не найден")
    for f, n in (("name", 200), ("unit", 32), ("description", 2000)):
        if f in a and str(a[f]).strip():
            setattr(m, f, str(a[f]).strip()[:n])
    if a.get("better") in ("up", "down"):
        m.better = a["better"]
    for f in ("warn_threshold", "bad_threshold"):
        if f in a:
            setattr(m, f, None if a[f] is None else float(a[f]))
    if "expected_days" in a:
        m.expected_days = max(1, min(int(a["expected_days"]), 400))
    ctx.db.commit()
    ctx.writes += 1
    return f"Показатель id={m.id} «{m.name}» обновлён"


def _agent_fields(ctx: Ctx, a: dict, ag: Agent) -> None:
    from .service import compute_next

    if "name" in a and str(a["name"]).strip():
        ag.name = str(a["name"]).strip()[:200]
    if "instruction" in a:
        ag.instruction = str(a["instruction"])[:4000]
    if "tools" in a:
        bad = [t for t in a["tools"] if t not in TOOLS]
        if bad:
            raise ToolError("Нет таких инструментов: " + ", ".join(map(str, bad)))
        ag.tools = json.dumps(list(a["tools"]))
    if a.get("level") in ("manager", "analyst", "ops"):
        ag.level = a["level"]
    if a.get("schedule_kind") in ("manual", "interval", "daily"):
        ag.schedule_kind = a["schedule_kind"]
    if "interval_minutes" in a:
        ag.interval_minutes = max(5, int(a["interval_minutes"])) if a["interval_minutes"] else None
    if "daily_time" in a:
        dt = a["daily_time"]
        if dt and not (len(str(dt)) == 5 and str(dt)[2] == ":" and str(dt)[:2].isdigit() and str(dt)[3:].isdigit()):
            raise ToolError("daily_time: ЧЧ:ММ по Москве")
        ag.daily_time = dt or None
    if "enabled" in a:
        ag.enabled = bool(a["enabled"])
    ag.next_run_at = compute_next(ag)


def create_agent(ctx: Ctx, a: dict) -> str:
    if not str(a.get("name", "")).strip() or not str(a.get("instruction", "")).strip():
        raise ToolError("Нужны name и instruction")
    ag = Agent(owner_id=_author(ctx), name="", kind="custom", instruction="", tools="[]", enabled=True, schedule_kind="manual", level="ops")
    _agent_fields(ctx, a, ag)
    ctx.db.add(ag)
    ctx.db.commit()
    ctx.writes += 1
    return f"Агент создан: id={ag.id}, «{ag.name}», инструменты: {ag.tools}"


def update_agent(ctx: Ctx, a: dict) -> str:
    ag = ctx.db.get(Agent, int(a.get("agent_id", 0)))
    if ag is None:
        raise ToolError("Агент не найден")
    _agent_fields(ctx, a, ag)
    ctx.db.commit()
    ctx.writes += 1
    return f"Агент id={ag.id} «{ag.name}» обновлён"


def run_agent_now(ctx: Ctx, a: dict) -> str:
    from .service import execute_run, start_run

    ag = ctx.db.get(Agent, int(a.get("agent_id", 0)))
    if ag is None:
        raise ToolError("Агент не найден")
    run = start_run(ctx.db, ag, "manual")
    threading.Thread(target=execute_run, args=(run.id,), daemon=True).start()
    ctx.writes += 1
    return f"Агент «{ag.name}» запущен (запуск id={run.id}). Итог появится в разделе «ИИ-агенты»"


def list_agents(ctx: Ctx, _a: dict) -> str:
    rows = [f"id={x.id} «{x.name}» уровень {x.level}, расписание {x.schedule_kind}, {'включён' if x.enabled else 'выключен'}, инструменты {x.tools}" for x in ctx.db.scalars(select(Agent).order_by(Agent.id))]
    return "\n".join(rows) or "Агентов нет"


def list_folders(ctx: Ctx, _a: dict) -> str:
    return "\n".join(f"id={f.id} «{f.name}» {f.description[:80]}" for f in ctx.db.scalars(select(Folder).order_by(Folder.name))) or "Папок нет"


def create_folder(ctx: Ctx, a: dict) -> str:
    name = str(a.get("name", "")).strip()
    if not name or len(name) > 255:
        raise ToolError("Нужно название папки")
    if ctx.db.scalar(select(Folder.id).where(Folder.name == name)):
        raise ToolError("Папка с таким названием уже есть")
    f = Folder(name=name, description=str(a.get("description", ""))[:1000], created_by=_author(ctx))
    ctx.db.add(f)
    ctx.db.commit()
    ctx.writes += 1
    return f"Папка создана: id={f.id}"


def save_file(ctx: Ctx, a: dict) -> str:
    from .. import search as search_index

    folder = ctx.db.get(Folder, int(a.get("folder_id", 0)))
    if folder is None:
        raise ToolError("Папка не найдена: смотрите list_folders")
    content = str(a.get("content", ""))
    name = Path(str(a.get("name", "")).strip()).name
    if not name or not content:
        raise ToolError("Нужны name и content")
    if not name.lower().endswith((".txt", ".csv", ".md")):
        name += ".txt"
    data = content.encode("utf-8")
    if len(data) > 300_000:
        raise ToolError("Файл больше 300 КБ")
    root = Path(settings.files_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    disk = uuid.uuid4().hex
    (root / disk).write_bytes(data)
    row = StoredFile(folder_id=folder.id, name=name[:255], size=len(data), content_type="text/plain; charset=utf-8", disk_name=disk, uploaded_by=_author(ctx))
    ctx.db.add(row)
    ctx.db.commit()
    search_index.index_file(ctx.db, row)
    ctx.writes += 1
    return f"Файл сохранён: file_id={row.id} в папке «{folder.name}»"


def save_supplier(ctx: Ctx, a: dict) -> str:
    name = str(a.get("name", "")).strip()
    if not name or len(name) > 200:
        raise ToolError("Нужно название поставщика")
    s = ctx.db.scalar(select(ReturnSupplier).where(ReturnSupplier.name == name))
    created = s is None
    if created:
        s = ReturnSupplier(name=name)
        ctx.db.add(s)
    for f, n in (("portal_url", 500), ("contact", 2000), ("rules", 4000), ("default_reason", 300), ("notes", 4000)):
        if f in a:
            setattr(s, f, str(a[f])[:n])
    s.is_active = True
    ctx.db.commit()
    ctx.writes += 1
    return f"Поставщик «{s.name}» {'создан' if created else 'обновлён'}: id={s.id}"


def run_integration_check(ctx: Ctx, a: dict) -> str:
    from ..integrations import service
    from ..integrations.registry import BY_KEY

    key = str(a.get("key", ""))
    if key not in BY_KEY:
        raise ToolError("Нет такой интеграции: " + ", ".join(BY_KEY))
    row = service.run_check(ctx.db, key)
    return f"{BY_KEY[key].name}: {row.status}. {row.message}"


def _cursor_cfg() -> dict:
    from ..integrations.checks import company_cfg

    cfg = company_cfg("cursor")
    if not cfg.get("api_key"):
        raise ToolError("Cursor не подключён: добавьте «Cursor (облачные агенты)» в «Интеграции → Подключения»")
    return cfg


def _cursor_call(method: str, path: str, body: dict | None = None) -> dict:
    import httpx

    cfg = _cursor_cfg()
    try:
        resp = httpx.request(method, "https://api.cursor.com/v0" + path, json=body, auth=(cfg["api_key"].strip(), ""), timeout=30.0)
    except httpx.HTTPError as exc:
        raise ToolError(f"Cursor не отвечает ({type(exc).__name__})") from None
    if resp.status_code in (401, 403):
        raise ToolError("Ключ Cursor недействителен или нет доступа")
    if resp.status_code >= 400:
        raise ToolError(f"Cursor ответил кодом {resp.status_code}: " + " ".join(resp.text.split())[:200])
    try:
        return resp.json()
    except ValueError:
        return {}


def cursor_launch(ctx: Ctx, a: dict) -> str:
    """Запуск облачного агента Cursor в репозитории из подключения. Он работает в своей ветке и открывает запрос на слияние: в main сам ничего не меняет."""
    prompt = str(a.get("prompt", "")).strip()
    if len(prompt) < 10 or len(prompt) > 8000:
        raise ToolError("Нужна понятная задача (от 10 до 8000 символов)")
    cfg = _cursor_cfg()
    data = _cursor_call("POST", "/agents", {"prompt": {"text": prompt}, "source": {"repository": cfg.get("repository") or "https://github.com/bonya242424/OperDirCLODEmirzap", "ref": cfg.get("ref") or "main"}, "target": {"autoCreatePr": True}})
    ctx.writes += 1
    return f"Агент Cursor запущен: id={data.get('id')}, статус {data.get('status')}, ветка {data.get('target', {}).get('branchName', '?')}, страница {data.get('target', {}).get('url', '')}"


def cursor_status(ctx: Ctx, a: dict) -> str:
    aid = str(a.get("agent_id", "")).strip()
    if not aid or "/" in aid:
        raise ToolError("Нужен id агента Cursor")
    d = _cursor_call("GET", f"/agents/{aid}")
    t = d.get("target", {})
    return f"id={d.get('id')} статус {d.get('status')}. {str(d.get('summary', ''))[:600]} Ветка {t.get('branchName', '?')}, страница {t.get('url', '')}"


def cursor_followup(ctx: Ctx, a: dict) -> str:
    aid, text = str(a.get("agent_id", "")).strip(), str(a.get("text", "")).strip()
    if not aid or "/" in aid or not text:
        raise ToolError("Нужны agent_id и text")
    _cursor_call("POST", f"/agents/{aid}/followup", {"prompt": {"text": text[:8000]}})
    ctx.writes += 1
    return "Уточнение отправлено агенту Cursor"


def cursor_list(ctx: Ctx, _a: dict) -> str:
    d = _cursor_call("GET", "/agents")
    rows = [f"id={x.get('id')} {x.get('name', '')} статус {x.get('status')}" for x in (d.get("agents") or [])[:20]]
    return "\n".join(rows) or "Агентов Cursor нет"


def bitrix_tasks(ctx: Ctx, a: dict) -> str:
    from .. import bitrix_watch as bw
    from ..bitrix import BitrixError

    try:
        d1, d2 = date.fromisoformat(str(a.get("date1"))), date.fromisoformat(str(a.get("date2")))
    except ValueError:
        raise ToolError("Даты в виде ГГГГ-ММ-ДД") from None
    if d2 < d1 or (d2 - d1).days > 400:
        raise ToolError("Период указан неверно (не больше 400 дней)")
    try:
        res = bw.list_period(ctx.db, d1, d2, str(a.get("kind") or "any"), int(a["bitrix_user_id"]) if a.get("bitrix_user_id") else None)
    except BitrixError as exc:
        raise ToolError(str(exc)) from None
    head = f"Задач: {res['count']}. По статусам: " + (", ".join(f"{k} {v}" for k, v in res["by_status"].items()) or "нет")
    rows = [
        f"#{t['id']} [{t['status_name']}] {t['title']} | исполнитель {t['responsible']} | создана {(t['created'] or '')[:10]} | срок {(t['deadline'] or '')[:10] or 'нет'} | закрыта {(t['closed'] or '')[:10] or '-'}"
        for t in res["tasks"][:150]
    ]
    return head + "\n" + "\n".join(rows) + ("\n…остальные не показаны, сузьте период" if res["count"] > 150 else "")


def bitrix_updates(ctx: Ctx, a: dict) -> str:
    from .. import bitrix_watch as bw

    days = int(a.get("days") or 7)
    ev = bw.recent_events(ctx.db, days, int(a["task_id"]) if a.get("task_id") else None, 150)
    if not ev:
        return f"За {days} дн. обновлений по задачам нет (или слежение ещё не накопило данные: первая проверка только запоминает текущее состояние)"
    return "\n".join(f"{e['at'][:16].replace('T', ' ')} #{e['task_id']} «{e['title'][:80]}» ({e['responsible']}): {e['text']}" for e in ev)


_S = {"type": "string"}
_N = {"type": "number"}
_I = {"type": "integer"}

OWNER_TOOLS = [
    Tool("create_task", "Создать задачу", "Создаёт задачу на доске. priority: low/normal/high; deadline ГГГГ-ММ-ДД; assignee_email сотрудника.", _obj({"title": _S, "description": _S, "priority": _S, "assignee_email": _S, "deadline": _S}, ["title"]), "write", create_task),
    Tool("update_task", "Изменить задачу", "Меняет задачу по id: title, description, priority, assignee_email, deadline, status (new/in_progress/review/done).", _obj({"task_id": _I, "title": _S, "description": _S, "priority": _S, "assignee_email": _S, "deadline": _S, "status": _S}, ["task_id"]), "write", update_task),
    Tool("update_metric", "Изменить показатель", "Меняет название, единицы, описание, пороги, срок ожидания показателя по id.", _obj({"metric_id": _I, "name": _S, "unit": _S, "description": _S, "better": _S, "warn_threshold": _N, "bad_threshold": _N, "expected_days": _I}, ["metric_id"]), "write", update_metric),
    Tool("list_agents", "Список агентов", "Агенты компании с id, расписанием и инструментами.", _obj({}), "read", list_agents),
    Tool("create_agent", "Создать агента", "Создаёт ИИ-агента: name, instruction, tools (имена инструментов), level manager/analyst/ops, schedule_kind manual/interval/daily, interval_minutes или daily_time ЧЧ:ММ по Москве.", _obj({"name": _S, "instruction": _S, "tools": {"type": "array", "items": _S}, "level": _S, "schedule_kind": _S, "interval_minutes": _I, "daily_time": _S}, ["name", "instruction"]), "write", create_agent),
    Tool("update_agent", "Изменить агента", "Меняет поручение, инструменты, расписание, включение агента по id.", _obj({"agent_id": _I, "name": _S, "instruction": _S, "tools": {"type": "array", "items": _S}, "level": _S, "schedule_kind": _S, "interval_minutes": _I, "daily_time": _S, "enabled": {"type": "boolean"}}, ["agent_id"]), "write", update_agent),
    Tool("run_agent_now", "Запустить агента", "Запускает агента сейчас по id; итог появится в «ИИ-агентах».", _obj({"agent_id": _I}, ["agent_id"]), "write", run_agent_now),
    Tool("list_folders", "Список папок", "Папки раздела «Файлы» с id.", _obj({}), "read", list_folders),
    Tool("create_folder", "Создать папку", "Создаёт папку в «Файлах».", _obj({"name": _S, "description": _S}, ["name"]), "write", create_folder),
    Tool("save_file", "Сохранить файл", "Сохраняет текстовый файл (txt, csv, md) в папку: например отчёт или выгрузку. Не больше 300 КБ.", _obj({"folder_id": _I, "name": _S, "content": _S}, ["folder_id", "name", "content"]), "write", save_file),
    Tool("save_supplier", "Создать или изменить поставщика", "Поставщик возвратов: name, portal_url, contact, rules, default_reason, notes. Существующий по названию обновляется.", _obj({"name": _S, "portal_url": _S, "contact": _S, "rules": _S, "default_reason": _S, "notes": _S}, ["name"]), "write", save_supplier),
    Tool("cursor_launch", "Запустить агента Cursor", "Ставит задачу облачному агенту Cursor в репозитории проекта. Он работает в отдельной ветке и открывает запрос на слияние, в main не пишет. Опишите задачу подробно.", _obj({"prompt": _S}, ["prompt"]), "write", cursor_launch),
    Tool("cursor_status", "Статус агента Cursor", "Статус и итог облачного агента Cursor по id.", _obj({"agent_id": _S}, ["agent_id"]), "read", cursor_status),
    Tool("cursor_followup", "Уточнить задачу агенту Cursor", "Отправляет дополнительное указание уже запущенному агенту Cursor.", _obj({"agent_id": _S, "text": _S}, ["agent_id", "text"]), "write", cursor_followup),
    Tool("cursor_list", "Список агентов Cursor", "Недавние облачные агенты Cursor.", _obj({}), "read", cursor_list),
    Tool("bitrix_tasks", "Задачи Битрикс24 за период", "Живые задачи выбранных сотрудников из Битрикс24 за период (любые статусы). date1, date2 в виде ГГГГ-ММ-ДД. kind: any (создана, закрыта или срок в периоде), created, closed, deadline, changed. bitrix_user_id: один сотрудник (по умолчанию все выбранные). Цифры берите только отсюда.", _obj({"date1": _S, "date2": _S, "kind": _S, "bitrix_user_id": _I}, ["date1", "date2"]), "read", bitrix_tasks),
    Tool("bitrix_updates", "Обновления по задачам Битрикс24", "Что изменилось по задачам за последние days дней (по умолчанию 7): новые, смена статуса, срока, исполнителя, комментарии. Копится проверкой раз в полчаса. task_id: по одной задаче.", _obj({"days": _I, "task_id": _I}), "read", bitrix_updates),
    Tool("run_integration_check", "Проверить интеграцию", "Проверяет интеграцию по ключу (например yandex_metrika, llm).", _obj({"key": _S}, ["key"]), "read", run_integration_check),
]
for _t in OWNER_TOOLS:
    TOOLS[_t.name] = _t
OWNER_TOOL_NAMES = [t.name for t in OWNER_TOOLS]
