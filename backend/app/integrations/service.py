import logging
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import Integration, IntegrationCheck, utcnow
from . import checks
from .registry import BY_KEY, REGISTRY, to_row

log = logging.getLogger(__name__)

HISTORY_DAYS = 30


def ensure_rows(db: Session) -> None:
    """Создаёт строки для новых интеграций и обновляет названия существующих."""
    existing = {row.key: row for row in db.scalars(select(Integration))}
    for defn in REGISTRY:
        row = existing.get(defn.key)
        if row is None:
            db.add(to_row(defn))
        else:
            row.name, row.description = defn.name, defn.description
    db.commit()


def run_check(db: Session, key: str) -> Integration:
    defn = BY_KEY[key]
    row = db.get(Integration, key)
    assert row is not None
    try:
        result = defn.check(db, row)
    except Exception as exc:  # noqa: BLE001 - сбой одной проверки не должен ронять остальные
        log.exception("Проверка интеграции %s упала", key)
        result = checks.CheckResult(checks.ERROR, f"Внутренняя ошибка проверки ({type(exc).__name__})")

    if row.status not in ("unknown", result.status):
        log.warning("Интеграция %s: статус %s -> %s (%s)", key, row.status, result.status, result.message)
    now = utcnow()
    row.status, row.message, row.last_check_at = result.status, result.message, now
    db.add(IntegrationCheck(integration_key=key, checked_at=now, status=result.status, message=result.message))
    db.commit()
    return row


def check_all(db: Session) -> list[Integration]:
    rows = [run_check(db, defn.key) for defn in REGISTRY]
    cutoff: datetime = utcnow() - timedelta(days=HISTORY_DAYS)
    db.execute(delete(IntegrationCheck).where(IntegrationCheck.checked_at < cutoff))
    db.commit()
    return rows


def run_scheduled_checks() -> None:
    """Точка входа для планировщика: своя сессия на каждый запуск."""
    with SessionLocal() as db:
        check_all(db)
        from ..routers.connections import check_all_connections

        check_all_connections(db)


def record_sync(db: Session, key: str, when: datetime | None = None) -> None:
    """Фиксирует успешную выгрузку данных. Вызывается синхронизациями и загрузкой файлов."""
    row = db.get(Integration, key)
    if row is None:
        raise KeyError(key)
    row.last_sync_at = when or utcnow()
    db.commit()
