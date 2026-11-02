import re
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user
from ..models import Proxy, User, utcnow
from ..proxies import check_proxy, seal_password
from ..schemas import _utc

router = APIRouter(prefix="/api/proxies", tags=["proxies"])
HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-]{0,253}[A-Za-z0-9])?$")


class ProxyIn(BaseModel):
    name: str = ""
    scheme: Literal["http", "https", "socks5"] = "http"
    host: str
    port: int
    username: str = ""
    password: str | None = None  # None при изменении: оставить прежний
    priority: int = 100
    enabled: bool = True

    @field_validator("host")
    @classmethod
    def _host(cls, v: str) -> str:
        v = v.strip()
        if not HOST_RE.match(v):
            raise ValueError("Адрес прокси: только домен или IP, без http:// и порта")
        return v

    @field_validator("port")
    @classmethod
    def _port(cls, v: int) -> int:
        if not 1 <= v <= 65535:
            raise ValueError("Порт от 1 до 65535")
        return v

    @field_validator("name", "username")
    @classmethod
    def _short(cls, v: str) -> str:
        v = v.strip()
        if len(v) > 200:
            raise ValueError("Слишком длинное значение")
        return v


class ProxyOut(BaseModel):
    id: int
    name: str
    scheme: str
    host: str
    port: int
    username: str
    has_password: bool
    priority: int
    enabled: bool
    last_status: str
    last_message: str
    last_ms: int | None
    last_check_at: datetime | None
    can_edit: bool

    _fix = field_validator("last_check_at")(_utc)


def _out(p: Proxy, me: User) -> ProxyOut:
    return ProxyOut(
        id=p.id, name=p.name, scheme=p.scheme, host=p.host, port=p.port, username=p.username, has_password=bool(p.password_encrypted),
        priority=p.priority, enabled=p.enabled, last_status=p.last_status, last_message=p.last_message, last_ms=p.last_ms,
        last_check_at=p.last_check_at, can_edit=me.role == "owner" or p.created_by == me.id,
    )


def _get(db: Session, proxy_id: int, me: User) -> Proxy:
    p = db.get(Proxy, proxy_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Прокси не найден")
    if me.role != "owner" and p.created_by != me.id:
        raise HTTPException(status_code=403, detail="Менять прокси может владелец или тот, кто его добавил")
    return p


@router.get("", response_model=list[ProxyOut])
def list_proxies(db: Session = Depends(get_db), me: User = Depends(current_user)):
    return [_out(p, me) for p in db.scalars(select(Proxy).order_by(Proxy.priority, Proxy.id))]


@router.post("", response_model=ProxyOut, status_code=201)
def create(body: ProxyIn, db: Session = Depends(get_db), me: User = Depends(current_user)):
    data = body.model_dump(exclude={"password"})
    p = Proxy(**data, password_encrypted=seal_password(body.password or ""), created_by=me.id)
    db.add(p)
    db.commit()
    return _out(p, me)


@router.put("/{proxy_id}", response_model=ProxyOut)
def update(proxy_id: int, body: ProxyIn, db: Session = Depends(get_db), me: User = Depends(current_user)):
    p = _get(db, proxy_id, me)
    for k, v in body.model_dump(exclude={"password"}).items():
        setattr(p, k, v)
    if body.password is not None:
        p.password_encrypted = seal_password(body.password)
    p.last_status, p.last_message = "unknown", ""
    db.commit()
    return _out(p, me)


@router.delete("/{proxy_id}", status_code=204)
def delete(proxy_id: int, db: Session = Depends(get_db), me: User = Depends(current_user)):
    db.delete(_get(db, proxy_id, me))
    db.commit()


@router.post("/{proxy_id}/check", response_model=ProxyOut)
def check(proxy_id: int, db: Session = Depends(get_db), me: User = Depends(current_user)):
    p = _get(db, proxy_id, me)
    ok, message, ms = check_proxy(p)
    p.last_status, p.last_message, p.last_ms, p.last_check_at = ("ok" if ok else "error"), message, ms, utcnow()
    db.commit()
    return _out(p, me)
