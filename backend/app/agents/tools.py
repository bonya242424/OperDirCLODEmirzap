"""Инструменты агентов. Что агент может делать, решает его создатель: он выбирает набор инструментов.

Инструмент чтения возвращает текст. Инструмент записи меняет данные сразу, но только если выдан агенту.
Инструмент с подтверждением ничего не меняет: он оставляет предложение, а выполняет его человек.
Всё, что инструмент возвращает модели, это данные, а не инструкции.
"""

import csv
import io
import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Agent, Folder, Metric, MetricValue, StoredFile, Task, User, utcnow
from ..routers.metrics import _out as metric_out

MAX_READ_CHARS = 6000
MAX_XLSX_ROWS = 200


class ToolError(Exception):
    """Понятная причина отказа: её увидит модель и сможет поправиться."""


@dataclass
class Ctx:
    db: Session
    agent: Agent
    log: Callable[[str], None]
    depth: int = 0
    run_id: int | None = None


@dataclass
class Tool:
    name: str
    title: str
    description: str
    schema: dict[str, Any]
    kind: str  # read | write | confirm
    handler: Callable[[Ctx, dict[str, Any]], str]
    describe: Callable[[dict[str, Any]], str] | None = None  # для confirm: что именно будет сделано
    execute: Callable[[Session, User, dict[str, Any]], str] | None = None  # для confirm: само действие


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or []}


# ---- чтение ----
def list_metrics(ctx: Ctx, _a: dict) -> str:
    rows = []
    for m in ctx.db.scalars(select(Metric).where(Metric.is_active.is_(True)).order_by(Metric.direction, Metric.id)):
        o = metric_out(ctx.db, m)
        rows.append(f"id={m.id} [{m.direction}] {m.name}: {o.latest if o.latest is not None else 'нет'} {m.unit} ({o.status}, период {o.latest_period})")
    return "\n".join(rows) or "Показателей нет"


def get_metric_history(ctx: Ctx, a: dict) -> str:
    mid = int(a.get("metric_id", 0))
    limit = max(1, min(int(a.get("limit", 30)), 90))
    if ctx.db.get(Metric, mid) is None:
        raise ToolError("Показатель не найден")
    vals = ctx.db.scalars(select(MetricValue).where(MetricValue.metric_id == mid).order_by(MetricValue.period.desc()).limit(limit))
    return "\n".join(f"{v.period}: {v.value}" for v in vals) or "Значений нет"


def list_files(ctx: Ctx, a: dict) -> str:
    q = select(StoredFile, Folder).join(Folder, Folder.id == StoredFile.folder_id).order_by(StoredFile.uploaded_at.desc()).limit(40)
    name = str(a.get("folder", "")).strip().lower()
    out = []
    for f, folder in ctx.db.execute(q):
        if name and name not in folder.name.lower():
            continue
        out.append(f"file_id={f.id} папка «{folder.name}» {f.name} {f.size // 1024} КБ загружен {f.uploaded_at.isoformat()}")
    return "\n".join(out) or "Файлов нет"


def _read_xlsx(path: Path) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    buf = io.StringIO()
    w = csv.writer(buf)
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i >= MAX_XLSX_ROWS:
            break
        w.writerow(["" if c is None else c for c in row])
    wb.close()
    return buf.getvalue()


def read_file(ctx: Ctx, a: dict) -> str:
    f = ctx.db.get(StoredFile, int(a.get("file_id", 0)))
    if f is None:
        raise ToolError("Файл не найден")
    path = Path(settings.files_dir).resolve() / f.disk_name
    if not path.is_file():
        raise ToolError("Файл на диске не найден")
    suffix = Path(f.name).suffix.lower()
    try:
        if suffix in (".xlsx", ".xlsm"):
            text = _read_xlsx(path)
        elif suffix in (".csv", ".txt", ".tsv", ".json", ".md", ""):
            text = path.read_bytes()[: MAX_READ_CHARS * 4].decode("utf-8-sig", errors="replace")
        else:
            raise ToolError(f"Формат «{suffix}» не читается: выгрузите файл как xlsx или csv")
    except ToolError:
        raise
    except Exception:  # noqa: BLE001
        raise ToolError("Файл не удалось прочитать: возможно, он повреждён") from None
    return text[:MAX_READ_CHARS] + ("\n…(обрезано)" if len(text) > MAX_READ_CHARS else "")


def list_tasks(ctx: Ctx, _a: dict) -> str:
    rows = ctx.db.scalars(select(Task).where(Task.status != "done").order_by(Task.id.desc()).limit(40))
    return "\n".join(f"id={t.id} [{t.status}] {t.title}" + (f" срок {t.deadline.date()}" if t.deadline else "") for t in rows) or "Открытых задач нет"


# ---- запись ----
def set_metric_value(ctx: Ctx, a: dict) -> str:
    m = ctx.db.get(Metric, int(a.get("metric_id", 0)))
    if m is None:
        raise ToolError("Показатель не найден")
    try:
        period = date.fromisoformat(str(a.get("period", "")))
        value = float(a.get("value"))
    except (TypeError, ValueError):
        raise ToolError("Нужны period в формате ГГГГ-ММ-ДД и число value") from None
    if period > utcnow().date() + timedelta(days=1):
        raise ToolError("Дата в будущем")
    note = f"Агент «{ctx.agent.name}»: {str(a.get('note', ''))[:200]}".strip()
    row = ctx.db.scalar(select(MetricValue).where(MetricValue.metric_id == m.id, MetricValue.period == period))
    if row is None:
        ctx.db.add(MetricValue(metric_id=m.id, period=period, value=value, note=note, created_by=ctx.agent.owner_id))
    else:
        row.value, row.note = value, note
    ctx.db.commit()
    return f"Записано: {m.name} за {period} = {value:g}"


def run_subordinate(ctx: Ctx, a: dict) -> str:
    from .runtime import run_agent  # циклический импорт: runtime использует инструменты

    child = ctx.db.get(Agent, int(a.get("agent_id", 0)))
    if child is None or child.parent_id != ctx.agent.id:
        raise ToolError("Можно запускать только своих подчинённых")
    if ctx.depth >= 2:
        raise ToolError("Слишком глубокая цепочка запусков")
    ctx.log(f"Запускаю подчинённого «{child.name}»")
    return run_agent(Ctx(ctx.db, child, ctx.log, ctx.depth + 1, ctx.run_id))


# ---- с подтверждением ----
def _task_describe(a: dict) -> str:
    return f"Создать задачу: {str(a.get('title', ''))[:200]}"


def _task_execute(db: Session, user: User, a: dict) -> str:
    title = str(a.get("title", "")).strip()[:300]
    if not title:
        raise ToolError("Пустое название задачи")
    assignee = None
    email = str(a.get("assignee_email", "")).strip().lower()
    if email:
        u = db.scalar(select(User).where(User.email == email))
        assignee = u.id if u else None
    t = Task(title=title, description=str(a.get("description", ""))[:4000], assignee_id=assignee, created_by=user.id, position=1e12)
    db.add(t)
    db.commit()
    return f"Создана задача «{title}»"


def propose_task(ctx: Ctx, a: dict) -> str:
    if not str(a.get("title", "")).strip():
        raise ToolError("Нужно название задачи")
    return "OK"  # само предложение сохраняет runtime как действие на подтверждение


TOOLS: dict[str, Tool] = {
    t.name: t
    for t in [
        Tool("list_metrics", "Список показателей", "Показатели компании с id, последним значением и статусом.", _obj({}), "read", list_metrics),
        Tool("get_metric_history", "История показателя", "Последние значения показателя по его id.", _obj({"metric_id": {"type": "integer"}, "limit": {"type": "integer"}}, ["metric_id"]), "read", get_metric_history),
        Tool("list_files", "Список файлов", "Файлы в разделе «Файлы», новые сверху. Можно указать часть названия папки.", _obj({"folder": {"type": "string"}}), "read", list_files),
        Tool("read_file", "Прочитать файл", "Содержимое файла csv, txt или xlsx (первый лист) по file_id.", _obj({"file_id": {"type": "integer"}}, ["file_id"]), "read", read_file),
        Tool("list_tasks", "Список задач", "Открытые задачи на доске.", _obj({}), "read", list_tasks),
        Tool("set_metric_value", "Записать значение показателя", "Записывает значение показателя за дату. Меняет данные сразу, перезаписывает значение за ту же дату.", _obj({"metric_id": {"type": "integer"}, "period": {"type": "string", "description": "ГГГГ-ММ-ДД"}, "value": {"type": "number"}, "note": {"type": "string"}}, ["metric_id", "period", "value"]), "write", set_metric_value),
        Tool("propose_task", "Предложить задачу", "Предлагает задачу. Создаётся только после подтверждения человеком.", _obj({"title": {"type": "string"}, "description": {"type": "string"}, "assignee_email": {"type": "string"}}, ["title"]), "confirm", propose_task, _task_describe, _task_execute),
        Tool("run_subordinate", "Запустить подчинённого", "Запускает своего подчинённого агента по id и возвращает его итог.", _obj({"agent_id": {"type": "integer"}}, ["agent_id"]), "read", run_subordinate),
    ]
}


def tool_defs(names: list[str]) -> list[dict[str, Any]]:
    return [{"name": t.name, "description": t.description, "input_schema": t.schema} for n in names if (t := TOOLS.get(n))]


def parse_tools(raw: str) -> list[str]:
    try:
        data = json.loads(raw or "[]")
    except ValueError:
        return []
    return [n for n in data if isinstance(n, str) and n in TOOLS]
