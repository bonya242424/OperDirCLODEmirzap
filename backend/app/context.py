"""Снимок данных компании для ИИ-чата: показатели, задачи, состояние интеграций.

Текст собирается из данных, которые вводят люди (названия задач, показателей), поэтому он
передаётся модели как данные и не как инструкции: см. SYSTEM_PROMPT в routers/chat.py.
"""

from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .integrations.registry import REGISTRY
from .models import BitrixTask, Integration, Metric, MetricValue, Task, User, utcnow
from .routers.metrics import _out as metric_out

DIRECTIONS = {"gen": "Генеральный директор", "tech": "Технический директор", "ops": "Операционный директор", "fin": "Финансовый директор"}
STATUS_RU = {"ok": "в норме", "warning": "внимание", "bad": "плохо", "no_data": "нет данных"}
INTEG_RU = {"ok": "работает", "warning": "внимание", "error": "ошибка", "not_configured": "не подключено", "unknown": "не проверялось"}
MAX_CHARS = 9000
MAX_TASKS = 15


def _clean(text: str, limit: int = 160) -> str:
    """Одна строка без управляющих символов: название задачи не должно ломать разметку снимка."""
    text = " ".join(text.split())
    return text[:limit]


def _fmt(v: float | None) -> str:
    return "нет" if v is None else f"{v:g}"


def _month_bounds(today: date) -> tuple[date, date, date]:
    this_start = today.replace(day=1)
    prev_end = this_start
    prev_start = (this_start.replace(day=1) - timedelta(days=1)).replace(day=1)
    return prev_start, prev_end, this_start


def _avg(db: Session, metric_id: int, start: date, end: date) -> tuple[float | None, int]:
    avg, n = db.execute(
        select(func.avg(MetricValue.value), func.count(MetricValue.id)).where(
            MetricValue.metric_id == metric_id, MetricValue.period >= start, MetricValue.period < end
        )
    ).one()
    return (None if avg is None else float(avg)), n


def build_context(db: Session) -> str:
    now = utcnow()
    lines: list[str] = [f"Сегодня {now.date().isoformat()} (UTC)."]

    lines.append("\n## Показатели по направлениям")
    metrics = list(db.scalars(select(Metric).where(Metric.is_active.is_(True)).order_by(Metric.direction, Metric.id)))
    if not metrics:
        lines.append("Показатели ещё не заведены.")
    for m in metrics:
        o = metric_out(db, m)
        part = f"- [{DIRECTIONS.get(m.direction, m.direction)}] {_clean(m.name)}: {_fmt(o.latest)} {m.unit}".rstrip()
        if o.latest_period:
            part += f" на {o.latest_period.isoformat()}"
        part += f", {STATUS_RU[o.status]}"
        if o.change is not None:
            part += f", изменение {o.change:+g}"
        if o.stale:
            part += ", данные устарели"
        if m.warn_threshold is not None or m.bad_threshold is not None:
            part += f" (пороги: внимание {_fmt(m.warn_threshold)}, плохо {_fmt(m.bad_threshold)}; {'больше лучше' if m.better == 'up' else 'меньше лучше'})"
        lines.append(part)
        prev_start, prev_end, this_start = _month_bounds(now.date())
        avg_prev, n_prev = _avg(db, m.id, prev_start, prev_end)
        avg_cur, n_cur = _avg(db, m.id, this_start, now.date() + timedelta(days=2))
        bits = []
        if avg_prev is not None:
            bits.append(f"среднее за прошлый месяц {avg_prev:.4g} (по {n_prev} значениям)")
        if avg_cur is not None:
            bits.append(f"среднее за текущий месяц {avg_cur:.4g} (по {n_cur} значениям)")
        if bits:
            lines.append("  " + "; ".join(bits))

    lines.append("\n## Задачи")
    names = {u.id: (u.full_name or u.email) for u in db.scalars(select(User))}
    bitrix_names = {u.bitrix_user_id: (u.full_name or u.email) for u in db.scalars(select(User)) if u.bitrix_user_id is not None}
    items: list[tuple[object, str, str, str]] = []  # (deadline, title, who, source)
    for t in db.scalars(select(Task).where(Task.status != "done")):
        items.append((t.deadline, t.title, names.get(t.assignee_id, "не назначен") if t.assignee_id else "не назначен", "доска"))
    for b in db.scalars(select(BitrixTask).where(BitrixTask.is_open.is_(True))):
        items.append((b.deadline, b.title, bitrix_names.get(b.responsible_id, f"id {b.responsible_id}"), "Битрикс24"))
    if not items:
        lines.append("Открытых задач нет.")
    else:
        def aware(d):
            return d if d is None or d.tzinfo else d.replace(tzinfo=now.tzinfo)

        overdue = [i for i in items if i[0] and aware(i[0]) < now]
        no_deadline = [i for i in items if not i[0]]
        lines.append(f"Открыто: {len(items)}, просрочено: {len(overdue)}, без срока: {len(no_deadline)}.")
        urgent = sorted((i for i in items if i[0]), key=lambda i: aware(i[0]))[:MAX_TASKS]
        for deadline, title, who, source in urgent:
            d = aware(deadline)
            tag = "ПРОСРОЧЕНА" if d < now else "срок"
            lines.append(f"- {tag} {d.date().isoformat()}: {_clean(title)} ({_clean(who, 60)}, {source})")

    lines.append("\n## Интеграции")
    rows = {r.key: r for r in db.scalars(select(Integration))}
    for d in REGISTRY:
        r = rows.get(d.key)
        if r is None:
            continue
        part = f"- {d.name}: {INTEG_RU.get(r.status, r.status)}"
        if r.last_sync_at:
            part += f", последняя выгрузка {r.last_sync_at.date().isoformat()}"
        if r.status in ("error", "warning") and r.message:
            part += f" ({_clean(r.message, 120)})"
        lines.append(part)

    text = "\n".join(lines)
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n[снимок обрезан]"
    return text
