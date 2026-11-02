"""Выгрузка недельной статистики Яндекс Метрики в показатели.

Значения кладутся в обычные показатели (MetricValue), чтобы цифры брались из базы, а ИИ их читал, а не придумывал.
Неделя считается с понедельника по воскресенье, в базу попадают только завершённые недели.
"""

import logging
import re
from datetime import date, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import SessionLocal
from .integrations.checks import _yandex_headers, _yandex_token, company_cfg
from .models import Metric, MetricValue, User, utcnow

log = logging.getLogger(__name__)

WEEKS = 12
# ключ источника, название, единицы, поле в ответе Метрики
SERIES = [
    ("yandex_metrika:visits", "Визиты на сайт (Метрика)", "визитов", "ym:s:visits"),
    ("yandex_metrika:users", "Посетители сайта (Метрика)", "чел.", "ym:s:users"),
    ("yandex_metrika:pageviews", "Просмотры страниц (Метрика)", "просмотров", "ym:s:pageviews"),
]


class MetrikaError(Exception):
    """Текст безопасен для показа."""


def week_starts(today: date, weeks: int = WEEKS) -> list[date]:
    """Понедельники последних завершённых недель, от старых к новым."""
    this_monday = today - timedelta(days=today.weekday())
    return [this_monday - timedelta(weeks=i) for i in range(weeks, 0, -1)]


def fetch_week(client: httpx.Client, counter: str, token: str, monday: date) -> list[float]:
    resp = client.get(
        "https://api-metrika.yandex.net/stat/v1/data",
        params={"ids": counter, "metrics": ",".join(s[3] for s in SERIES), "date1": monday.isoformat(), "date2": (monday + timedelta(days=6)).isoformat(), "accuracy": "full"},
        headers=_yandex_headers(token),
    )
    if resp.status_code in (401, 403):
        raise MetrikaError("Токен недействителен или нет доступа к счётчику")
    if resp.status_code != 200:
        raise MetrikaError(f"Метрика ответила кодом {resp.status_code}")
    totals = resp.json().get("totals") or []
    if len(totals) != len(SERIES):
        raise MetrikaError("Метрика вернула неожиданный ответ")
    return [float(v or 0) for v in totals]


def _metric(db: Session, key: str, name: str, unit: str, owner_id: int) -> Metric:
    m = db.scalar(select(Metric).where(Metric.source_kind == key))
    if m is None:
        m = Metric(direction="gen", name=name, unit=unit, description="Подтягивается из Яндекс Метрики автоматически, раз в неделю по завершённым неделям (пн–вс)", better="up", expected_days=8, created_by=owner_id, source_kind=key)
        db.add(m)
        db.flush()
    return m


def sync_metrika(db: Session, today: date | None = None) -> int:
    """Возвращает число записанных значений. Бросает MetrikaError с понятным текстом."""
    token = _yandex_token("yandex_metrika")
    counter = company_cfg("yandex_metrika").get("counter_id", "").strip() or settings.yandex_metrika_counter_id.strip()
    if not token or not counter.isdigit():
        raise MetrikaError("Не заданы токен и номер счётчика Метрики")
    owner = db.scalar(select(User).where(User.role == "owner").order_by(User.id))
    if owner is None:
        raise MetrikaError("В системе нет владельца")
    rows: dict[date, list[float]] = {}
    try:
        with httpx.Client(timeout=20.0) as client:
            for monday in week_starts(today or date.today()):
                rows[monday] = fetch_week(client, counter, token, monday)
    except httpx.HTTPError as exc:
        raise MetrikaError(f"Ошибка сети ({type(exc).__name__})") from None
    written = 0
    for i, (key, name, unit, _) in enumerate(SERIES):
        m = _metric(db, key, name, unit, owner.id)
        have = {v.period: v for v in db.scalars(select(MetricValue).where(MetricValue.metric_id == m.id))}
        for monday, vals in rows.items():
            note = f"Яндекс Метрика, неделя {monday:%d.%m}–{monday + timedelta(days=6):%d.%m.%Y}"
            if monday in have:
                have[monday].value, have[monday].note = vals[i], note
            else:
                db.add(MetricValue(metric_id=m.id, period=monday, value=vals[i], note=note, created_by=owner.id))
            written += 1
    db.commit()
    return written


def run_scheduled_metrika() -> None:
    from .integrations.service import record_sync

    with SessionLocal() as db:
        if not _yandex_token("yandex_metrika"):
            return
        try:
            n = sync_metrika(db) + sync_custom(db)
        except MetrikaError as exc:
            log.warning("Выгрузка Метрики не удалась: %s", exc)
            return
        except Exception:  # noqa: BLE001
            log.exception("Выгрузка Метрики упала")
            return
        record_sync(db, "yandex_metrika")
        log.info("Метрика: записано значений %s", n)


_NAME = re.compile(r"^ym:(s|pv):[A-Za-z0-9]+$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _names(value, what: str, limit: int) -> list[str]:
    if isinstance(value, str):
        value = [v.strip() for v in value.split(",") if v.strip()]
    value = list(value or [])
    if len(value) > limit or any(not isinstance(v, str) or not _NAME.match(v) for v in value):
        raise MetrikaError(f"{what}: имена вида ym:s:visits (не больше {limit})")
    return value


def query_stat(a: dict) -> str:
    """Произвольный запрос к Метрике за любой период. Возвращает таблицу текстом."""
    token = _yandex_token("yandex_metrika")
    counter = company_cfg("yandex_metrika").get("counter_id", "").strip() or settings.yandex_metrika_counter_id.strip()
    if not token or not counter.isdigit():
        raise MetrikaError("Метрика не подключена: нужны токен и номер счётчика")
    date1, date2 = str(a.get("date1", "")), str(a.get("date2", ""))
    if not (_DATE.match(date1) and _DATE.match(date2)):
        raise MetrikaError("Даты нужны в виде ГГГГ-ММ-ДД")
    metrics = _names(a.get("metrics") or ["ym:s:visits", "ym:s:users", "ym:s:pageviews"], "Показатели", 20)
    dims = _names(a.get("dimensions"), "Разрезы", 10)
    params = {"ids": counter, "metrics": ",".join(metrics), "date1": date1, "date2": date2, "accuracy": "full", "limit": max(1, min(int(a.get("limit", 100)), 500))}
    if dims:
        params["dimensions"] = ",".join(dims)
    sort = str(a.get("sort", "")).strip()
    if sort:
        if not re.match(r"^-?ym:(s|pv):[A-Za-z0-9]+$", sort):
            raise MetrikaError("Сортировка: ym:s:visits или -ym:s:visits")
        params["sort"] = sort
    flt = str(a.get("filters", "")).strip()
    if flt:
        params["filters"] = flt[:500]
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.get("https://api-metrika.yandex.net/stat/v1/data", params=params, headers=_yandex_headers(token))
    except httpx.HTTPError as exc:
        raise MetrikaError(f"Ошибка сети ({type(exc).__name__})") from None
    if resp.status_code in (401, 403):
        raise MetrikaError("Токен недействителен или нет доступа к счётчику")
    if resp.status_code == 400:
        msg = ""
        try:
            msg = str(resp.json().get("message", ""))[:200]
        except Exception:  # noqa: BLE001
            pass
        raise MetrikaError(f"Метрика не приняла запрос: {msg or 'проверьте имена показателей и разрезов'}")
    if resp.status_code != 200:
        raise MetrikaError(f"Метрика ответила кодом {resp.status_code}")
    body = resp.json()
    lines = [f"Метрика, {date1} – {date2}. Колонки: " + ", ".join(dims + metrics)]
    for row in body.get("data", []):
        names = [str(d.get("name") or d.get("id") or "") for d in row.get("dimensions", [])]
        lines.append(" | ".join(names + [f"{v:g}" if isinstance(v, (int, float)) else str(v) for v in row.get("metrics", [])]))
    totals = body.get("totals") or []
    if totals:
        lines.append("ИТОГО: " + " | ".join(f"{v:g}" if isinstance(v, (int, float)) else str(v) for v in totals))
    if body.get("sampled"):
        lines.append("Внимание: данные семплированы")
    if body.get("total_rows", 0) > len(body.get("data", [])):
        lines.append(f"Показано {len(body.get('data', []))} строк из {body['total_rows']}: сузьте период или добавьте фильтр")
    return "\n".join(lines)


# ---- показатели с произвольным запросом к Метрике (создаёт ИИ или человек) ----
QUERY_KIND = "yandex_metrika:query"


def parse_spec(m: Metric) -> dict:
    import json

    try:
        spec = json.loads(m.source_prompt)
    except ValueError:
        raise MetrikaError("Описание источника показателя повреждено") from None
    if not isinstance(spec, dict) or not _NAME.match(str(spec.get("metric", ""))):
        raise MetrikaError("В описании источника нет метрики вида ym:s:visits")
    spec["period"] = spec.get("period") if spec.get("period") in ("week", "month") else "week"
    spec["count"] = max(1, min(int(spec.get("count") or 12), 60))
    spec["filters"] = str(spec.get("filters") or "")[:500]
    return spec


def month_starts(today: date, count: int) -> list[date]:
    first = today.replace(day=1)
    out = []
    for _ in range(count):
        first = (first - timedelta(days=1)).replace(day=1)
        out.append(first)
    return out[::-1]


def periods(spec: dict, today: date) -> list[tuple[date, date]]:
    if spec["period"] == "month":
        return [(d, (d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)) for d in month_starts(today, spec["count"])]
    return [(d, d + timedelta(days=6)) for d in week_starts(today, spec["count"])]


def sync_one(db: Session, m: Metric, today: date | None = None) -> int:
    """Пересчитывает один показатель из Метрики за последние завершённые периоды."""
    spec = parse_spec(m)
    token = _yandex_token("yandex_metrika")
    counter = company_cfg("yandex_metrika").get("counter_id", "").strip() or settings.yandex_metrika_counter_id.strip()
    if not token or not counter.isdigit():
        raise MetrikaError("Не заданы токен и номер счётчика Метрики")
    have = {v.period: v for v in db.scalars(select(MetricValue).where(MetricValue.metric_id == m.id))}
    n = 0
    try:
        with httpx.Client(timeout=30.0) as client:
            for d1, d2 in periods(spec, today or date.today()):
                params = {"ids": counter, "metrics": spec["metric"], "date1": d1.isoformat(), "date2": d2.isoformat(), "accuracy": "full"}
                if spec["filters"]:
                    params["filters"] = spec["filters"]
                resp = client.get("https://api-metrika.yandex.net/stat/v1/data", params=params, headers=_yandex_headers(token))
                if resp.status_code in (401, 403):
                    raise MetrikaError("Токен недействителен или нет доступа к счётчику")
                if resp.status_code == 400:
                    raise MetrikaError("Метрика не приняла запрос: проверьте имя метрики и фильтр")
                if resp.status_code != 200:
                    raise MetrikaError(f"Метрика ответила кодом {resp.status_code}")
                totals = resp.json().get("totals") or [0]
                val = float(totals[0] or 0)
                note = f"Яндекс Метрика {spec['metric']}, {d1:%d.%m}–{d2:%d.%m.%Y}" + (f", фильтр: {spec['filters']}" if spec["filters"] else "")
                if d1 in have:
                    have[d1].value, have[d1].note = val, note
                else:
                    db.add(MetricValue(metric_id=m.id, period=d1, value=val, note=note, created_by=m.created_by))
                n += 1
    except httpx.HTTPError as exc:
        raise MetrikaError(f"Ошибка сети ({type(exc).__name__})") from None
    db.commit()
    return n


def sync_custom(db: Session) -> int:
    total = 0
    for m in db.scalars(select(Metric).where(Metric.source_kind == QUERY_KIND, Metric.is_active.is_(True))):
        try:
            total += sync_one(db, m)
        except MetrikaError as exc:
            m.calc_status, m.calc_message = "error", str(exc)
            db.commit()
        else:
            m.calc_status, m.calc_message, m.calc_at = "ok", "", utcnow()
            db.commit()
    return total
