"""Удалённые окна: обычный браузер на сервере и терминал (RDP). Только владелец.

Картинка идёт в интерфейс, клики и нажатия клавиш идут обратно. Пароль терминала берётся из зашифрованного подключения
и в ответах API не появляется.
"""

import re

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import connections as conn_lib
from ..browser import VIEW_H, VIEW_W, BrowserError, manager
from ..db import get_db
from ..deps import current_user
from ..models import Connection, User
from ..rdp import H as RDP_H, W as RDP_W, rdp_manager

router = APIRouter(prefix="/api/remote", tags=["remote"])
FREE_BROWSER_BASE = 3_000_000
RDP_BASE = 4_000_000


def _owner(me: User) -> None:
    if me.role != "owner":
        raise HTTPException(status_code=403, detail="Удалённые окна открывает только владелец")


def _wrap(fn):
    try:
        return fn()
    except BrowserError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


def _conn(db: Session, conn_id: int) -> Connection:
    row = db.get(Connection, conn_id)
    if row is None or row.kind != "rdp" or row.owner_id is not None:
        raise HTTPException(status_code=404, detail="Подключение к терминалу не найдено")
    return row


@router.get("/targets")
def targets(db: Session = Depends(get_db), me: User = Depends(current_user)):
    _owner(me)
    rows = db.scalars(select(Connection).where(Connection.kind == "rdp", Connection.owner_id.is_(None)).order_by(Connection.title))
    return {
        "browser": {"open": manager.is_open(FREE_BROWSER_BASE + me.id)},
        "rdp": [{"id": c.id, "title": c.title, "host": conn_lib.decrypt(c.config_encrypted).get("host", ""), "open": rdp_manager.is_open(RDP_BASE + c.id)} for c in rows],
    }


class OpenIn(BaseModel):
    url: str = ""

    @field_validator("url")
    @classmethod
    def _u(cls, v: str) -> str:
        v = v.strip()
        if v and not re.match(r"^https?://[^\s]+$", v):
            raise ValueError("Адрес должен начинаться с http:// или https://")
        return v[:1000]


class ClickIn(BaseModel):
    x: float
    y: float
    button: str = "left"
    double: bool = False


class TypeIn(BaseModel):
    text: str

    @field_validator("text")
    @classmethod
    def _t(cls, v: str) -> str:
        if not v or len(v) > 1000:
            raise ValueError("Текст: от 1 до 1000 символов")
        return v


class KeyIn(BaseModel):
    key: str


class ScrollIn(BaseModel):
    dy: float


class HistoryIn(BaseModel):
    action: str


# --- обычный браузер на сервере (один на владельца) ---
@router.post("/browser/open")
def browser_open(body: OpenIn, me: User = Depends(current_user)):
    _owner(me)
    key = FREE_BROWSER_BASE + me.id
    _wrap(lambda: manager.open(key, body.url or "https://www.google.com/", None))
    return {"width": VIEW_W, "height": VIEW_H, "address": True}


@router.post("/browser/navigate", status_code=204)
def browser_navigate(body: OpenIn, me: User = Depends(current_user)):
    _owner(me)
    if not body.url:
        raise HTTPException(status_code=422, detail="Введите адрес")
    _wrap(lambda: manager.navigate(FREE_BROWSER_BASE + me.id, body.url))


@router.post("/browser/history", status_code=204)
def browser_history(body: HistoryIn, me: User = Depends(current_user)):
    _owner(me)
    _wrap(lambda: manager.history(FREE_BROWSER_BASE + me.id, body.action))


@router.get("/browser/screen")
def browser_screen(me: User = Depends(current_user)):
    _owner(me)
    return Response(content=_wrap(lambda: manager.screenshot(FREE_BROWSER_BASE + me.id)), media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.get("/browser/info")
def browser_info(me: User = Depends(current_user)):
    _owner(me)
    return _wrap(lambda: manager.info(FREE_BROWSER_BASE + me.id))


@router.post("/browser/click", status_code=204)
def browser_click(body: ClickIn, me: User = Depends(current_user)):
    _owner(me)
    _wrap(lambda: manager.click(FREE_BROWSER_BASE + me.id, body.x, body.y, body.button, body.double))


@router.post("/browser/type", status_code=204)
def browser_type(body: TypeIn, me: User = Depends(current_user)):
    _owner(me)
    _wrap(lambda: manager.type_text(FREE_BROWSER_BASE + me.id, body.text))


@router.post("/browser/key", status_code=204)
def browser_key(body: KeyIn, me: User = Depends(current_user)):
    _owner(me)
    _wrap(lambda: manager.press(FREE_BROWSER_BASE + me.id, body.key))


@router.post("/browser/scroll", status_code=204)
def browser_scroll(body: ScrollIn, me: User = Depends(current_user)):
    _owner(me)
    _wrap(lambda: manager.scroll(FREE_BROWSER_BASE + me.id, body.dy))


@router.post("/browser/close", status_code=204)
def browser_close(me: User = Depends(current_user)):
    _owner(me)
    _wrap(lambda: manager.close(FREE_BROWSER_BASE + me.id))


# --- терминал RDP: подключение по данным из «Интеграции → Подключения» ---
@router.post("/rdp/{conn_id}/open")
def rdp_open(conn_id: int, db: Session = Depends(get_db), me: User = Depends(current_user)):
    _owner(me)
    row = _conn(db, conn_id)
    cfg = conn_lib.decrypt(row.config_encrypted)
    if not cfg.get("host") or not cfg.get("user") or not cfg.get("password"):
        raise HTTPException(status_code=422, detail="В подключении не заполнены адрес, логин или пароль: нажмите «Изменить» в «Интеграции → Подключения»")
    try:
        port = int(cfg.get("port") or 3389)
    except ValueError:
        raise HTTPException(status_code=422, detail="Порт должен быть числом") from None
    info = _wrap(lambda: rdp_manager.open(RDP_BASE + row.id, cfg["host"].strip(), port, cfg["user"], cfg["password"], cfg.get("domain", "").strip()))
    return {**info, "address": False}


@router.get("/rdp/{conn_id}/screen")
def rdp_screen(conn_id: int, db: Session = Depends(get_db), me: User = Depends(current_user)):
    _owner(me)
    _conn(db, conn_id)
    return Response(content=_wrap(lambda: rdp_manager.screenshot(RDP_BASE + conn_id)), media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.get("/rdp/{conn_id}/info")
def rdp_info(conn_id: int, db: Session = Depends(get_db), me: User = Depends(current_user)):
    _owner(me)
    row = _conn(db, conn_id)
    _wrap(lambda: rdp_manager.screenshot(RDP_BASE + conn_id) if not rdp_manager.is_open(RDP_BASE + conn_id) else None)
    return {"url": row.title, "title": "Терминал"}


@router.post("/rdp/{conn_id}/click", status_code=204)
def rdp_click(conn_id: int, body: ClickIn, db: Session = Depends(get_db), me: User = Depends(current_user)):
    _owner(me)
    _conn(db, conn_id)
    _wrap(lambda: rdp_manager.click(RDP_BASE + conn_id, body.x, body.y, body.button, body.double))


@router.post("/rdp/{conn_id}/type", status_code=204)
def rdp_type(conn_id: int, body: TypeIn, db: Session = Depends(get_db), me: User = Depends(current_user)):
    _owner(me)
    _conn(db, conn_id)
    _wrap(lambda: rdp_manager.type_text(RDP_BASE + conn_id, body.text))


@router.post("/rdp/{conn_id}/key", status_code=204)
def rdp_key(conn_id: int, body: KeyIn, db: Session = Depends(get_db), me: User = Depends(current_user)):
    _owner(me)
    _conn(db, conn_id)
    _wrap(lambda: rdp_manager.press(RDP_BASE + conn_id, body.key))


@router.post("/rdp/{conn_id}/scroll", status_code=204)
def rdp_scroll(conn_id: int, body: ScrollIn, db: Session = Depends(get_db), me: User = Depends(current_user)):
    _owner(me)
    _conn(db, conn_id)
    _wrap(lambda: rdp_manager.scroll(RDP_BASE + conn_id, body.dy))


@router.post("/rdp/{conn_id}/close", status_code=204)
def rdp_close(conn_id: int, db: Session = Depends(get_db), me: User = Depends(current_user)):
    _owner(me)
    _conn(db, conn_id)
    rdp_manager.close(RDP_BASE + conn_id)
