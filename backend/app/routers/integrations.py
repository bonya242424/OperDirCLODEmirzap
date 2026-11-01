from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..deps import current_user
from ..integrations import service
from ..integrations.registry import BY_KEY, REGISTRY
from ..models import Integration, IntegrationCheck, User
from ..schemas import IntegrationCheckOut, IntegrationOut, IntegrationsOut

router = APIRouter(prefix="/api/integrations", tags=["integrations"])


def _out(row: Integration) -> IntegrationOut:
    return IntegrationOut(
        key=row.key,
        name=row.name,
        description=row.description,
        status=row.status,
        message=row.message,
        has_sync=BY_KEY[row.key].has_sync,
        last_check_at=row.last_check_at,
        last_sync_at=row.last_sync_at,
    )


def _all(rows: list[Integration]) -> IntegrationsOut:
    order = {d.key: i for i, d in enumerate(REGISTRY)}
    items = sorted((r for r in rows if r.key in order), key=lambda r: order[r.key])
    return IntegrationsOut(
        check_interval_minutes=settings.integration_check_minutes,
        items=[_out(r) for r in items],
    )


@router.get("", response_model=IntegrationsOut)
def list_integrations(db: Session = Depends(get_db), _: User = Depends(current_user)):
    return _all(list(db.scalars(select(Integration))))


@router.post("/check", response_model=IntegrationsOut)
def check_all(db: Session = Depends(get_db), _: User = Depends(current_user)):
    return _all(service.check_all(db))


@router.post("/{key}/check", response_model=IntegrationOut)
def check_one(key: str, db: Session = Depends(get_db), _: User = Depends(current_user)):
    if key not in BY_KEY:
        raise HTTPException(status_code=404, detail="Интеграция не найдена")
    return _out(service.run_check(db, key))


@router.get("/{key}/history", response_model=list[IntegrationCheckOut])
def history(key: str, limit: int = 48, db: Session = Depends(get_db), _: User = Depends(current_user)):
    if key not in BY_KEY:
        raise HTTPException(status_code=404, detail="Интеграция не найдена")
    limit = max(1, min(limit, 500))
    return db.scalars(
        select(IntegrationCheck)
        .where(IntegrationCheck.integration_key == key)
        .order_by(IntegrationCheck.checked_at.desc(), IntegrationCheck.id.desc())
        .limit(limit)
    ).all()
