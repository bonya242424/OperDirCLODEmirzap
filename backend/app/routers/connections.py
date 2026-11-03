from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import connections as conn_lib
from ..db import get_db
from ..deps import current_user
from ..models import Connection, User, utcnow
from ..schemas import _utc

router = APIRouter(prefix="/api/connections", tags=["connections"])


class ConnectionIn(BaseModel):
    kind: str
    scope: Literal["company", "personal"] = "personal"
    title: str = ""
    config: dict[str, str] = {}


class ConnectionPatch(BaseModel):
    title: str | None = None
    # Секретные поля, оставленные пустыми, не меняются.
    config: dict[str, str] | None = None


class ConnectionOut(BaseModel):
    id: int
    kind: str
    kind_name: str
    title: str
    scope: str
    owner_id: int | None
    owner_name: str
    status: str
    message: str
    last_check_at: datetime | None
    last_sync_at: datetime | None
    # Несекретные значения; секретные никогда не возвращаются.
    values: dict[str, str]
    secrets_set: list[str]
    can_edit: bool

    _fix = field_validator("last_check_at", "last_sync_at")(_utc)


def _out(row: Connection, me: User, names: dict[int, str]) -> ConnectionOut:
    spec = conn_lib.KINDS[row.kind]
    cfg = conn_lib.decrypt(row.config_encrypted)
    return ConnectionOut(
        id=row.id,
        kind=row.kind,
        kind_name=spec.name,
        title=row.title,
        scope="company" if row.owner_id is None else "personal",
        owner_id=row.owner_id,
        owner_name="Компания" if row.owner_id is None else names.get(row.owner_id, ""),
        status=row.status,
        message=row.message,
        last_check_at=row.last_check_at,
        last_sync_at=row.last_sync_at,
        values={f.name: cfg.get(f.name, "") for f in spec.fields if not f.secret},
        secrets_set=[f.name for f in spec.fields if f.secret and cfg.get(f.name)],
        can_edit=_can_edit(row, me),
    )


def _can_edit(row: Connection, me: User) -> bool:
    return me.role == "owner" or row.created_by == me.id or row.owner_id == me.id


def _names(db: Session) -> dict[int, str]:
    return {u.id: (u.full_name or u.email) for u in db.scalars(select(User))}


def _clean(kind: str, config: dict[str, str]) -> dict[str, str]:
    spec = conn_lib.KINDS[kind]
    allowed = {f.name for f in spec.fields}
    unknown = set(config) - allowed
    if unknown:
        raise HTTPException(status_code=422, detail="Неизвестные поля: " + ", ".join(sorted(unknown)))
    return {k: v.strip() for k, v in config.items() if k in allowed}


def run_check(db: Session, row: Connection) -> Connection:
    result = conn_lib.check_connection(row.kind, conn_lib.decrypt(row.config_encrypted))
    row.status, row.message, row.last_check_at = result.status, result.message, utcnow()
    db.commit()
    return row


def check_all_connections(db: Session) -> None:
    for row in db.scalars(select(Connection)).all():
        run_check(db, row)


def _get(db: Session, conn_id: int) -> Connection:
    row = db.get(Connection, conn_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Подключение не найдено")
    return row


@router.get("/kinds")
def kinds(_: User = Depends(current_user)):
    return [
        {
            "key": k.key,
            "name": k.name,
            "description": k.description,
            "fields": [
                {"name": f.name, "label": f.label, "secret": f.secret, "required": f.required, "default": f.default}
                for f in k.fields
            ],
        }
        for k in conn_lib.KINDS.values()
    ]


@router.get("", response_model=list[ConnectionOut])
def list_connections(db: Session = Depends(get_db), me: User = Depends(current_user)):
    names = _names(db)
    rows = db.scalars(select(Connection).order_by(Connection.owner_id.is_not(None), Connection.id)).all()
    return [_out(r, me, names) for r in rows]


@router.post("", response_model=ConnectionOut, status_code=201)
def create(body: ConnectionIn, db: Session = Depends(get_db), me: User = Depends(current_user)):
    if body.kind not in conn_lib.KINDS:
        raise HTTPException(status_code=422, detail="Неизвестный тип подключения")
    if body.kind == "rdp":
        if me.role != "owner":
            raise HTTPException(status_code=403, detail="Доступ к терминалу добавляет только владелец")
        body = body.model_copy(update={"scope": "company"})
    if body.kind in ("anthropic", "openai", "cursor"):
        if me.role != "owner":
            raise HTTPException(status_code=403, detail="Ключ модели добавляет только владелец")
        if db.scalar(select(Connection.id).where(Connection.kind == body.kind, Connection.owner_id.is_(None))):
            raise HTTPException(status_code=409, detail="Ключ уже добавлен: измените его в существующем подключении")
        body = body.model_copy(update={"scope": "company"})
    config = _clean(body.kind, body.config)
    for f in conn_lib.KINDS[body.kind].fields:
        if not config.get(f.name) and f.default:
            config[f.name] = f.default
    row = Connection(
        kind=body.kind,
        title=body.title.strip() or conn_lib.KINDS[body.kind].name,
        owner_id=me.id if body.scope == "personal" else None,
        created_by=me.id,
        config_encrypted=conn_lib.encrypt(config),
    )
    db.add(row)
    db.commit()
    return _out(run_check(db, row), me, _names(db))


@router.patch("/{conn_id}", response_model=ConnectionOut)
def update(conn_id: int, body: ConnectionPatch, db: Session = Depends(get_db), me: User = Depends(current_user)):
    row = _get(db, conn_id)
    if not _can_edit(row, me):
        raise HTTPException(status_code=403, detail="Менять это подключение может его владелец")
    if body.title is not None and body.title.strip():
        row.title = body.title.strip()
    if body.config is not None:
        merged = conn_lib.decrypt(row.config_encrypted)
        for k, v in _clean(row.kind, body.config).items():
            if v or not next(f.secret for f in conn_lib.KINDS[row.kind].fields if f.name == k):
                merged[k] = v
        row.config_encrypted = conn_lib.encrypt(merged)
    db.commit()
    return _out(run_check(db, row), me, _names(db))


@router.delete("/{conn_id}", status_code=204)
def delete(conn_id: int, db: Session = Depends(get_db), me: User = Depends(current_user)):
    row = _get(db, conn_id)
    if not _can_edit(row, me):
        raise HTTPException(status_code=403, detail="Удалить это подключение может его владелец")
    db.delete(row)
    db.commit()


@router.post("/{conn_id}/check", response_model=ConnectionOut)
def check(conn_id: int, db: Session = Depends(get_db), me: User = Depends(current_user)):
    return _out(run_check(db, _get(db, conn_id)), me, _names(db))
