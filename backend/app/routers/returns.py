"""Возвраты поставщикам: загрузка таблицы со сборкой и саммари по каждому поставщику.

Все числа в саммари считает код по строкам таблицы, модель их не придумывает.
"""

import csv
import io
import re
from collections import defaultdict

import httpx
import json
import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Response, UploadFile
from pydantic import BaseModel, ValidationError, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user
from .. import connections as conn_lib
from ..browser import VIEW_H, VIEW_W, BrowserError, manager
from ..config import settings
from ..guide_calc import analyze_guide, guides_dir
from ..models import ReturnBatch, ReturnGuide, ReturnLine, ReturnSupplier, User, utcnow

router = APIRouter(prefix="/api/returns", tags=["returns"])

MAX_BYTES = 5 * 1024 * 1024
MAX_LINES = 2000
SHEET_RE = re.compile(r"^https://docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]{20,})")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


COLS = {
    "article": ("товар", "артикул", "код"),
    "name": ("наименование",),
    "brand": ("производитель", "бренд"),
    "qty": ("кол. по документу", "количество", "кол-во", "кол."),
    "warehouse": ("начальный склад возврата", "склад"),
    "invoice_date": ("дата счета", "дата счёта"),
    "invoice_no": ("номер счета", "номер счёта"),
    "reason": ("причина возврата", "причина"),
}


def parse_sheet(text: str, need_supplier: bool = True) -> tuple[str, str, list[dict]]:
    """Возвращает (поставщик, номер сборки, строки). Бросает ValueError с понятным текстом."""
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    supplier, number = "", ""
    header_at = -1
    for i, r in enumerate(rows):
        first = _norm(r[0]) if r else ""
        if first == "поставщик" and len(r) > 1:
            supplier = r[1].strip()
        elif first == "сборка" and len(r) > 1:
            number = r[1].strip()
        elif first == "товар" and any(_norm(c).startswith("кол") for c in r):
            header_at = i
            break
    if header_at < 0:
        raise ValueError("Не нашли строку заголовков: нужны колонки «Товар» и «Кол. по документу»")
    if need_supplier and not supplier:
        raise ValueError("В таблице нет строки «Поставщик»")
    index: dict[str, int] = {}
    for key, names in COLS.items():
        for j, c in enumerate(rows[header_at]):
            if _norm(c) in names:
                index[key] = j
                break
    for need, label in (("article", "Товар"), ("qty", "Кол. по документу"), ("invoice_no", "Номер счета")):
        if need not in index:
            raise ValueError(f"Нет колонки «{label}»")
    lines: list[dict] = []
    for r in rows[header_at + 1 :]:
        def get(key: str) -> str:
            j = index.get(key)
            return r[j].strip() if j is not None and j < len(r) else ""

        if not get("article"):
            continue
        qty_raw = get("qty").replace(",", ".")
        try:
            qty = int(float(qty_raw))
        except ValueError:
            raise ValueError(f"Строка «{get('article')}»: количество «{get('qty')}» не число") from None
        if qty <= 0:
            raise ValueError(f"Строка «{get('article')}»: количество должно быть больше нуля")
        lines.append(
            {k: (qty if k == "qty" else get(k)) for k in COLS}
        )
    if not lines:
        raise ValueError("В таблице нет строк с товарами")
    if len(lines) > MAX_LINES:
        raise ValueError(f"Слишком много строк (больше {MAX_LINES})")
    return supplier, number, lines


def _supplier_by_name(db: Session, name: str, me: User) -> ReturnSupplier:
    sup = db.scalar(select(ReturnSupplier).where(ReturnSupplier.name == name))
    if sup is None:
        sup = ReturnSupplier(name=name[:200], created_by=me.id)
        db.add(sup)
        db.flush()
    return sup


def _save(db: Session, text: str, source: str, me: User, supplier: ReturnSupplier | None = None) -> ReturnBatch:
    try:
        name, number, lines = parse_sheet(text, need_supplier=supplier is None)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    sup = supplier or _supplier_by_name(db, name, me)
    batch = ReturnBatch(supplier=sup.name, supplier_id=sup.id, number=number[:200], source=source[:500], created_by=me.id)
    db.add(batch)
    db.flush()
    for ln in lines:
        row = {k: (v[:500] if isinstance(v, str) else v) for k, v in ln.items()}
        if not row.get("reason") and sup.default_reason:
            row["reason"] = sup.default_reason
        db.add(ReturnLine(batch_id=batch.id, **row))
    db.commit()
    return batch


class UrlIn(BaseModel):
    url: str
    supplier_id: int | None = None


@router.post("/import", status_code=201)
async def import_file(supplier_id: int | None = None, file: UploadFile = File(...), db: Session = Depends(get_db), me: User = Depends(current_user)):
    sup = _get_supplier(db, supplier_id) if supplier_id else None
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="Файл больше 5 МБ")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=422, detail="Нужен CSV в кодировке UTF-8: в Google Таблицах «Файл → Скачать → CSV»") from None
    return _summary(db, _save(db, text, file.filename or "файл", me, sup))


@router.post("/import-url", status_code=201)
def import_url(body: UrlIn, db: Session = Depends(get_db), me: User = Depends(current_user)):
    sup = _get_supplier(db, body.supplier_id) if body.supplier_id else None
    m = SHEET_RE.match(body.url.strip())
    if not m:
        raise HTTPException(status_code=422, detail="Нужна ссылка на Google Таблицу вида docs.google.com/spreadsheets/d/…")
    sheet_id = m.group(1)
    gid = re.search(r"[#?&]gid=(\d+)", body.url)
    export = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv" + (f"&gid={gid.group(1)}" if gid else "")
    try:
        resp = httpx.get(export, timeout=20.0, follow_redirects=True)
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="Не удалось открыть таблицу: проверьте ссылку и доступ «по ссылке»") from None
    if resp.status_code != 200 or "text/csv" not in resp.headers.get("content-type", ""):
        raise HTTPException(status_code=502, detail="Таблица недоступна: откройте доступ «Все, у кого есть ссылка» с правом просмотра или редактирования")
    if len(resp.content) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="Таблица больше 5 МБ")
    return _summary(db, _save(db, resp.content.decode("utf-8-sig"), f"Google Таблица {sheet_id[:8]}…", me, sup))


def _summary(db: Session, batch: ReturnBatch) -> dict:
    lines = list(db.scalars(select(ReturnLine).where(ReturnLine.batch_id == batch.id).order_by(ReturnLine.id)))
    by_inv: dict[str, dict] = defaultdict(lambda: {"positions": 0, "qty": 0, "date": ""})
    by_wh: dict[str, dict] = defaultdict(lambda: {"positions": 0, "qty": 0})
    by_reason: dict[str, int] = defaultdict(int)
    by_status: dict[str, int] = defaultdict(int)
    for ln in lines:
        i = by_inv[ln.invoice_no or "без номера"]
        i["positions"] += 1
        i["qty"] += ln.qty
        i["date"] = ln.invoice_date
        w = by_wh[ln.warehouse or "не указан"]
        w["positions"] += 1
        w["qty"] += ln.qty
        by_reason[ln.reason or "не указана"] += ln.qty
        by_status[ln.status] += 1
    problems = []
    no_reason = sum(1 for ln in lines if not ln.reason)
    if no_reason:
        problems.append(f"У {no_reason} строк нет причины возврата")
    no_inv = sum(1 for ln in lines if not ln.invoice_no)
    if no_inv:
        problems.append(f"У {no_inv} строк нет номера счёта")
    return {
        "id": batch.id,
        "supplier_id": batch.supplier_id,
        "supplier": batch.supplier,
        "number": batch.number,
        "source": batch.source,
        "created_at": batch.created_at.isoformat(),
        "positions": len(lines),
        "qty": sum(ln.qty for ln in lines),
        "invoices": [{"invoice_no": k, **v} for k, v in sorted(by_inv.items())],
        "warehouses": [{"warehouse": k, **v} for k, v in sorted(by_wh.items(), key=lambda kv: -kv[1]["qty"])],
        "reasons": [{"reason": k, "qty": v} for k, v in by_reason.items()],
        "status": dict(by_status),
        "problems": problems,
    }


@router.get("/batches")
def batches(supplier_id: int | None = None, db: Session = Depends(get_db), _: User = Depends(current_user)):
    q = select(ReturnBatch).order_by(ReturnBatch.id.desc()).limit(100)
    if supplier_id is not None:
        q = q.where(ReturnBatch.supplier_id == supplier_id)
    rows = db.scalars(q)
    return [_summary(db, b) for b in rows]


@router.get("/batches/{batch_id}/lines")
def lines(batch_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    if db.get(ReturnBatch, batch_id) is None:
        raise HTTPException(status_code=404, detail="Сборка не найдена")
    rows = db.scalars(select(ReturnLine).where(ReturnLine.batch_id == batch_id).order_by(ReturnLine.id))
    return [
        {c: getattr(r, c) for c in ("id", "article", "name", "brand", "qty", "warehouse", "invoice_date", "invoice_no", "reason", "status", "request_no", "note")}
        for r in rows
    ]


@router.delete("/batches/{batch_id}", status_code=204)
def delete_batch(batch_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    b = db.get(ReturnBatch, batch_id)
    if b is None:
        raise HTTPException(status_code=404, detail="Сборка не найдена")
    for r in db.scalars(select(ReturnLine).where(ReturnLine.batch_id == batch_id)):
        db.delete(r)
    db.delete(b)
    db.commit()


class SupplierIn(BaseModel):
    name: str
    portal_url: str = ""
    contact: str = ""
    rules: str = ""
    default_reason: str = ""
    notes: str = ""

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) > 200:
            raise ValueError("Название поставщика: от 1 до 200 символов")
        return v

    @field_validator("portal_url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = v.strip()
        if v and not re.match(r"^https?://[^\s]+$", v):
            raise ValueError("Адрес сайта должен начинаться с http:// или https://")
        return v[:500]

    @field_validator("contact", "rules", "notes")
    @classmethod
    def _long(cls, v: str) -> str:
        return v.strip()[:4000]

    @field_validator("default_reason")
    @classmethod
    def _reason(cls, v: str) -> str:
        return v.strip()[:300]


class SupplierPatch(BaseModel):
    name: str | None = None
    portal_url: str | None = None
    contact: str | None = None
    rules: str | None = None
    default_reason: str | None = None
    notes: str | None = None


def _get_supplier(db: Session, supplier_id: int) -> ReturnSupplier:
    sup = db.get(ReturnSupplier, supplier_id)
    if sup is None or not sup.is_active:
        raise HTTPException(status_code=404, detail="Поставщик не найден")
    return sup


def _supplier_out(db: Session, sup: ReturnSupplier) -> dict:
    ids = list(db.scalars(select(ReturnBatch.id).where(ReturnBatch.supplier_id == sup.id)))
    positions = db.scalar(select(func.count(ReturnLine.id)).where(ReturnLine.batch_id.in_(ids))) if ids else 0
    return {
        "id": sup.id, "name": sup.name, "portal_url": sup.portal_url, "contact": sup.contact, "rules": sup.rules,
        "default_reason": sup.default_reason, "notes": sup.notes, "batches": len(ids), "positions": positions or 0,
        "has_session": bool(sup.session_encrypted), "session_saved_at": sup.session_saved_at.isoformat() if sup.session_saved_at else None,
        "browser_open": manager.is_open(sup.id),
    }


@router.get("/suppliers")
def list_suppliers(db: Session = Depends(get_db), _: User = Depends(current_user)):
    return [_supplier_out(db, s) for s in db.scalars(select(ReturnSupplier).where(ReturnSupplier.is_active.is_(True)).order_by(ReturnSupplier.name))]


@router.post("/suppliers", status_code=201)
def create_supplier(body: SupplierIn, db: Session = Depends(get_db), me: User = Depends(current_user)):
    if db.scalar(select(ReturnSupplier).where(ReturnSupplier.name == body.name)):
        raise HTTPException(status_code=409, detail="Поставщик с таким названием уже есть")
    sup = ReturnSupplier(**body.model_dump(), created_by=me.id)
    db.add(sup)
    db.commit()
    return _supplier_out(db, sup)


@router.patch("/suppliers/{supplier_id}")
def update_supplier(supplier_id: int, body: SupplierPatch, db: Session = Depends(get_db), _: User = Depends(current_user)):
    sup = _get_supplier(db, supplier_id)
    data = body.model_dump(exclude_unset=True)
    try:
        merged = SupplierIn(**{**{k: getattr(sup, k) for k in SupplierIn.model_fields}, **{k: v for k, v in data.items() if v is not None}})
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()[0]["msg"].removeprefix("Value error, ")) from None
    if merged.name != sup.name and db.scalar(select(ReturnSupplier).where(ReturnSupplier.name == merged.name)):
        raise HTTPException(status_code=409, detail="Поставщик с таким названием уже есть")
    for k, v in merged.model_dump().items():
        setattr(sup, k, v)
    db.commit()
    return _supplier_out(db, sup)


@router.delete("/suppliers/{supplier_id}", status_code=204)
def archive_supplier(supplier_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    """Поставщик скрывается, загруженные документы остаются."""
    _get_supplier(db, supplier_id).is_active = False
    db.commit()


VIDEO_EXT = {".mp4", ".mov", ".webm", ".m4v", ".mkv"}


def _guide_out(g: ReturnGuide) -> dict:
    return {
        "id": g.id, "supplier_id": g.supplier_id, "filename": g.filename, "size": g.size, "status": g.status,
        "text": g.text, "message": g.message, "created_at": g.created_at.isoformat(),
    }


def _get_guide(db: Session, guide_id: int) -> ReturnGuide:
    g = db.get(ReturnGuide, guide_id)
    if g is None:
        raise HTTPException(status_code=404, detail="Видео не найдено")
    return g


@router.get("/suppliers/{supplier_id}/guides")
def list_guides(supplier_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    _get_supplier(db, supplier_id)
    return [_guide_out(g) for g in db.scalars(select(ReturnGuide).where(ReturnGuide.supplier_id == supplier_id).order_by(ReturnGuide.id.desc()))]


@router.post("/suppliers/{supplier_id}/guides", status_code=201)
async def upload_guide(
    supplier_id: int,
    tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    me: User = Depends(current_user),
):
    _get_supplier(db, supplier_id)
    name = (file.filename or "видео").replace("\\", "/").split("/")[-1][:300]
    if Path(name).suffix.lower() not in VIDEO_EXT:
        raise HTTPException(status_code=422, detail="Нужен видеофайл: mp4, mov, webm, m4v или mkv")
    limit = settings.max_upload_mb * 1024 * 1024
    disk_name = uuid.uuid4().hex
    target = guides_dir() / disk_name
    size = 0
    try:
        with open(target, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(status_code=413, detail=f"Видео больше {settings.max_upload_mb} МБ")
                out.write(chunk)
        if size == 0:
            raise HTTPException(status_code=422, detail="Файл пустой")
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    g = ReturnGuide(supplier_id=supplier_id, filename=name, disk_name=disk_name, size=size, created_by=me.id)
    db.add(g)
    db.commit()
    tasks.add_task(analyze_guide, g.id)
    db.refresh(g)
    return _guide_out(g)


class GuideText(BaseModel):
    text: str

    @field_validator("text")
    @classmethod
    def _t(cls, v: str) -> str:
        return v.strip()[:20000]


@router.patch("/guides/{guide_id}")
def edit_guide(guide_id: int, body: GuideText, db: Session = Depends(get_db), _: User = Depends(current_user)):
    g = _get_guide(db, guide_id)
    g.text = body.text
    db.commit()
    return _guide_out(g)


@router.post("/guides/{guide_id}/reanalyze", status_code=202)
def reanalyze_guide(guide_id: int, tasks: BackgroundTasks, db: Session = Depends(get_db), _: User = Depends(current_user)):
    g = _get_guide(db, guide_id)
    if g.status == "running":
        raise HTTPException(status_code=409, detail="Разбор уже идёт")
    g.status, g.message = "running", ""
    db.commit()
    tasks.add_task(analyze_guide, g.id)
    return _guide_out(g)


@router.delete("/guides/{guide_id}", status_code=204)
def delete_guide(guide_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    g = _get_guide(db, guide_id)
    (guides_dir() / g.disk_name).unlink(missing_ok=True)
    db.delete(g)
    db.commit()


# --- Браузер на сервере: владелец сам входит в кабинет, мы сохраняем только cookies сессии ---

def _owner(me: User) -> None:
    if me.role != "owner":
        raise HTTPException(status_code=403, detail="Вход в кабинет поставщика выполняет владелец")


def _browser(fn):
    try:
        return fn()
    except BrowserError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.post("/suppliers/{supplier_id}/browser/open")
def browser_open(supplier_id: int, db: Session = Depends(get_db), me: User = Depends(current_user)):
    _owner(me)
    sup = _get_supplier(db, supplier_id)
    if not sup.portal_url:
        raise HTTPException(status_code=422, detail="У поставщика не указан адрес кабинета: заполните его в «Данные поставщика»")
    state = None
    if sup.session_encrypted:
        raw = conn_lib.decrypt(sup.session_encrypted).get("state")
        state = json.loads(raw) if raw else None
    _browser(lambda: manager.open(sup.id, sup.portal_url, state))
    return {"width": VIEW_W, "height": VIEW_H}


@router.get("/suppliers/{supplier_id}/browser/screen")
def browser_screen(supplier_id: int, db: Session = Depends(get_db), me: User = Depends(current_user)):
    _owner(me)
    _get_supplier(db, supplier_id)
    img = _browser(lambda: manager.screenshot(supplier_id))
    return Response(content=img, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.get("/suppliers/{supplier_id}/browser/info")
def browser_info(supplier_id: int, db: Session = Depends(get_db), me: User = Depends(current_user)):
    _owner(me)
    _get_supplier(db, supplier_id)
    return _browser(lambda: manager.info(supplier_id))


class ClickIn(BaseModel):
    x: float
    y: float


class TypeIn(BaseModel):
    text: str

    @field_validator("text")
    @classmethod
    def _t(cls, v: str) -> str:
        if not v or len(v) > 500:
            raise ValueError("Текст: от 1 до 500 символов")
        return v


class KeyIn(BaseModel):
    key: str


class ScrollIn(BaseModel):
    dy: float


@router.post("/suppliers/{supplier_id}/browser/click", status_code=204)
def browser_click(supplier_id: int, body: ClickIn, db: Session = Depends(get_db), me: User = Depends(current_user)):
    _owner(me)
    _get_supplier(db, supplier_id)
    _browser(lambda: manager.click(supplier_id, body.x, body.y))


@router.post("/suppliers/{supplier_id}/browser/type", status_code=204)
def browser_type(supplier_id: int, body: TypeIn, db: Session = Depends(get_db), me: User = Depends(current_user)):
    """Текст уходит прямо в страницу: он может быть паролем, поэтому нигде не сохраняется и не логируется."""
    _owner(me)
    _get_supplier(db, supplier_id)
    _browser(lambda: manager.type_text(supplier_id, body.text))


@router.post("/suppliers/{supplier_id}/browser/key", status_code=204)
def browser_key(supplier_id: int, body: KeyIn, db: Session = Depends(get_db), me: User = Depends(current_user)):
    _owner(me)
    _get_supplier(db, supplier_id)
    _browser(lambda: manager.press(supplier_id, body.key))


@router.post("/suppliers/{supplier_id}/browser/scroll", status_code=204)
def browser_scroll(supplier_id: int, body: ScrollIn, db: Session = Depends(get_db), me: User = Depends(current_user)):
    _owner(me)
    _get_supplier(db, supplier_id)
    _browser(lambda: manager.scroll(supplier_id, body.dy))


@router.post("/suppliers/{supplier_id}/browser/save")
def browser_save(supplier_id: int, db: Session = Depends(get_db), me: User = Depends(current_user)):
    """Сохраняет cookies текущего окна: дальше агент работает под этим входом."""
    _owner(me)
    sup = _get_supplier(db, supplier_id)
    state = _browser(lambda: manager.state(supplier_id))
    sup.session_encrypted = conn_lib.encrypt({"state": json.dumps(state)})
    sup.session_saved_at = utcnow()
    db.commit()
    return _supplier_out(db, sup)


@router.post("/suppliers/{supplier_id}/browser/close", status_code=204)
def browser_close(supplier_id: int, db: Session = Depends(get_db), me: User = Depends(current_user)):
    _owner(me)
    _get_supplier(db, supplier_id)
    _browser(lambda: manager.close(supplier_id))


@router.delete("/suppliers/{supplier_id}/browser/session", status_code=204)
def browser_forget(supplier_id: int, db: Session = Depends(get_db), me: User = Depends(current_user)):
    """Забыть сохранённый вход: потребуется войти заново."""
    _owner(me)
    sup = _get_supplier(db, supplier_id)
    sup.session_encrypted, sup.session_saved_at = "", None
    db.commit()
