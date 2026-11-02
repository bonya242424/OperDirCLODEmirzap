"""Возвраты поставщикам: загрузка таблицы со сборкой и саммари по каждому поставщику.

Все числа в саммари считает код по строкам таблицы, модель их не придумывает.
"""

import csv
import io
import re
from collections import defaultdict

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, ValidationError, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user
from ..models import ReturnBatch, ReturnLine, ReturnSupplier, User

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
