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
from datetime import date
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import assembly_returns as ar
from ..db import get_db
from ..deps import current_user
from .. import connections as conn_lib
from ..browser import VIEW_H, VIEW_W, BrowserError, manager
from ..config import settings
from ..guide_calc import analyze_guide, guides_dir
from ..models import ReturnBatch, ReturnGuide, ReturnLine, ReturnRun, ReturnSupplier, User, utcnow
from ..returns_run import run_check
from ..return_workers import execute_runs, split_approval
from ..return_jobs import create_job, run_job, load_queue, stop_jobs
from ..models import ReturnJob
from ..return_submission import LOCKED_STATUSES, RUN_LOCK, ACTIVE_QUEUES, APPROVAL_PREFIX, eligible_rows, approval_log, supplier_busy, supplier_kind

router = APIRouter(prefix="/api/returns", tags=["returns"])

MAX_BYTES = 5 * 1024 * 1024
MAX_LINES = 2000
VIDEO_EXT = {".mp4", ".mov", ".webm", ".m4v", ".mkv"}
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


DOC_DATE_RE = re.compile(r"от\s+(\d{1,2})\.(\d{1,2})\.(\d{4})")


def doc_date_of(number: str) -> date | None:
    m = DOC_DATE_RE.search(number or "")
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def backfill_doc_dates() -> None:
    """Старые документы без даты сборки: берём её из номера, иначе из даты загрузки."""
    from ..db import SessionLocal

    with SessionLocal() as db:
        for b in db.scalars(select(ReturnBatch).where(ReturnBatch.doc_date.is_(None))):
            b.doc_date = doc_date_of(b.number) or b.created_at.date()
        db.commit()


def _save(db: Session, text: str, source: str, me: User, supplier: ReturnSupplier | None = None) -> ReturnBatch:
    try:
        name, number, lines = parse_sheet(text, need_supplier=supplier is None)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    sup = supplier or ar.supplier_by_name(db, name, me.id)
    try:
        return ar.store_batch(db, sup, number, lines, source, me.id)
    except ar.Locked as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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
    by_inv: dict[str, dict] = defaultdict(lambda: {"positions": 0, "qty": 0, "date": "", "status": "pending", "note": ""})
    by_wh: dict[str, dict] = defaultdict(lambda: {"positions": 0, "qty": 0})
    by_reason: dict[str, int] = defaultdict(int)
    by_status: dict[str, int] = defaultdict(int)
    for ln in lines:
        i = by_inv[ln.invoice_no or "без номера"]
        i["positions"] += 1
        i["qty"] += ln.qty
        i["date"] = ln.invoice_date
        if ln.status != "pending":
            i["status"], i["note"] = ln.status, ln.note
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
        "doc_date": batch.doc_date.isoformat() if batch.doc_date else None,
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
def batches(
    supplier_id: int | None = None, date_from: date | None = None, date_to: date | None = None,
    db: Session = Depends(get_db), _: User = Depends(current_user),
):
    """Документы (сборки по неделям). Период отбирает по дате сборки."""
    q = select(ReturnBatch).order_by(ReturnBatch.doc_date.desc(), ReturnBatch.id.desc()).limit(100)
    if supplier_id is not None:
        q = q.where(ReturnBatch.supplier_id == supplier_id)
    if date_from is not None:
        q = q.where(ReturnBatch.doc_date >= date_from)
    if date_to is not None:
        q = q.where(ReturnBatch.doc_date <= date_to)
    rows = db.scalars(q)
    return [_summary(db, b) for b in rows]


@router.post("/batches/{batch_id}/reset-errors")
def reset_errors(batch_id: int, db: Session = Depends(get_db), me: User = Depends(current_user)):
    with RUN_LOCK:
        """Сбросить неудачные проверки выбранной сборки для повторного запуска."""
        _owner(me)
        batch = db.get(ReturnBatch, batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="Сборка не найдена")
        if _QUEUES.get(batch.supplier_id, {}).get("active") or db.scalar(
            select(ReturnRun.id).join(ReturnBatch, ReturnBatch.id == ReturnRun.batch_id)
            .where(ReturnBatch.supplier_id == batch.supplier_id, ReturnRun.status == "running")
        ):
            raise HTTPException(status_code=409, detail="Сначала остановите проверку и очередь этого поставщика")
        rows = list(db.scalars(select(ReturnLine).where(ReturnLine.batch_id == batch_id, ReturnLine.status == "failed")))
        reset = 0
        for row in rows:
            if not row.request_no:
                row.status = "pending"
                row.note = ""
                reset += 1
        db.commit()
        return {"reset": reset}


@router.post("/dedupe")
def dedupe_batches(db: Session = Depends(get_db), me: User = Depends(current_user)):
    """Убрать дубли документов (один и тот же номер у поставщика). Остаётся тот, где больше результатов проверки."""
    _owner(me)
    return ar.dedupe(db)


class ReconcileIn(BaseModel):
    outcome: Literal["not_sent", "submitted"]
    verified: bool = False
    request_no: str = ""
    note: str

    @field_validator("note")
    @classmethod
    def _note(cls, value: str) -> str:
        if not 5 <= len(value.strip()) <= 1000:
            raise ValueError("Опишите результат сверки истории: от 5 до 1000 символов")
        return value.strip()

    @field_validator("request_no")
    @classmethod
    def _request(cls, value: str) -> str:
        if len(value.strip()) > 100:
            raise ValueError("Номер заявки слишком длинный")
        return value.strip()


@router.post("/lines/{line_id}/reconcile")
def reconcile_line(line_id: int, data: ReconcileIn, db: Session = Depends(get_db), me: User = Depends(current_user)):
    _owner(me)
    if not data.verified:
        raise HTTPException(status_code=422, detail="Сначала сверьте историю возвратов в кабинете поставщика")
    if data.outcome == "submitted" and not data.request_no:
        raise HTTPException(status_code=422, detail="Укажите номер найденной заявки")
    if data.outcome == "not_sent" and data.request_no:
        raise HTTPException(status_code=422, detail="Для найденной заявки выберите «Возврат оформлен»")
    with RUN_LOCK:
        row = db.get(ReturnLine, line_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Товарная строка не найдена")
        batch = db.get(ReturnBatch, row.batch_id)
        if supplier_busy(db, batch.supplier_id):
            raise HTTPException(status_code=409, detail="Сначала остановите запуск и очередь этого поставщика")
        if row.status != "uncertain" or row.request_no:
            raise HTTPException(status_code=409, detail="Сверка доступна только для неподтверждённой отправки")
        run = db.scalar(select(ReturnRun).where(ReturnRun.batch_id == row.batch_id).order_by(ReturnRun.id.desc()).limit(1))
        if run is None:
            raise HTTPException(status_code=409, detail="Нет журнала попытки отправки для сохранения сверки")
        audit = {
            "line_id": row.id, "article": row.article, "brand": row.brand, "qty": row.qty,
            "outcome": data.outcome, "request_no": data.request_no, "note": data.note,
            "previous_note": row.note, "owner_id": me.id, "at": utcnow().isoformat(),
        }
        entries = json.loads(run.log or "[]")
        entries.append("Сверка отправки владельцем: " + json.dumps(audit, ensure_ascii=False))
        run.log = json.dumps(entries, ensure_ascii=False)
        row.status = "submitted" if data.outcome == "submitted" else "pending"
        row.request_no = data.request_no
        row.note = f"Сверено владельцем {audit['at']}: {data.note}. " + (
            f"Возврат оформлен, заявка {data.request_no}. Повторная отправка запрещена."
            if data.outcome == "submitted" else "Заявка в истории отсутствует; разрешён новый запуск оформления."
        )
        db.commit()
        return {"line_id": row.id, "status": row.status, "request_no": row.request_no}


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
    with RUN_LOCK:
        b = db.get(ReturnBatch, batch_id)
        if b is None:
            raise HTTPException(status_code=404, detail="Сборка не найдена")
        if supplier_busy(db, b.supplier_id):
            raise HTTPException(status_code=409, detail="Сначала остановите запуск и очередь поставщика")
        if db.scalar(select(ReturnLine.id).where(
            ReturnLine.batch_id == batch_id,
            (ReturnLine.status.in_(LOCKED_STATUSES)) | (ReturnLine.request_no != ""),
        ).limit(1)):
            raise HTTPException(status_code=409, detail="Сборка содержит отправленные заявки или незавершённую отправку. Удаление запрещено.")
        for r in db.scalars(select(ReturnLine).where(ReturnLine.batch_id == batch_id)):
            db.delete(r)
        for r in db.scalars(select(ReturnRun).where(ReturnRun.batch_id == batch_id)):
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
    ids = list(db.scalars(select(ReturnBatch.id).where(ReturnBatch.supplier_id == sup.id).order_by(ReturnBatch.id.desc())))
    # каждый документ это отдельная неделя возвратов: числа не суммируем, показываем последний
    positions = db.scalar(select(func.count(ReturnLine.id)).where(ReturnLine.batch_id == ids[0])) if ids else 0
    last_run = db.scalar(
        select(ReturnRun).join(ReturnBatch, ReturnBatch.id == ReturnRun.batch_id)
        .where(ReturnBatch.supplier_id == sup.id)
        .order_by((ReturnRun.status == "running").desc(), ReturnRun.id.desc()).limit(1)
    )
    load_queue(db, sup.id)
    check_status = "not_started"
    if _QUEUES.get(sup.id, {}).get("active") or (last_run and last_run.status == "running"):
        check_status = "running"
    elif last_run and last_run.status == "failed":
        check_status = "error"
    elif last_run and last_run.status == "done":
        unresolved_statuses = ("failed", "uncertain", "submitting", "pending", "checked") if last_run.mode == "submit" else ("failed", "uncertain", "submitting", "pending")
        has_errors = db.scalar(
            select(ReturnLine.id).where(ReturnLine.batch_id == last_run.batch_id, ReturnLine.status.in_(unresolved_statuses)).limit(1)
        )
        check_status = "error" if has_errors else "done"
    return {
        "check_status": check_status,
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
    with RUN_LOCK:
        sup = _get_supplier(db, supplier_id)
        if supplier_busy(db, supplier_id):
            raise HTTPException(status_code=409, detail="Дождитесь завершения запуска перед изменением поставщика")
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
    with RUN_LOCK:
        """Поставщик скрывается, загруженные документы остаются."""
        sup = _get_supplier(db, supplier_id)
        if supplier_busy(db, supplier_id) or db.scalar(
            select(ReturnLine.id).join(ReturnBatch, ReturnBatch.id == ReturnLine.batch_id)
            .where(ReturnBatch.supplier_id == supplier_id, (ReturnLine.status.in_(LOCKED_STATUSES)) | (ReturnLine.request_no != "")).limit(1)
        ):
            raise HTTPException(status_code=409, detail="У поставщика есть оформленные возвраты или активный запуск. Скрытие запрещено.")
        sup.is_active = False
        db.commit()




def _guide_out(g: ReturnGuide) -> dict:
    return {
        "id": g.id, "supplier_id": g.supplier_id, "filename": g.filename, "size": g.size, "status": g.status,
        "text": g.text, "message": g.message, "created_at": g.created_at.isoformat(), "has_video": bool(g.disk_name and g.size),
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
    if not g.disk_name or not g.size:
        raise HTTPException(status_code=409, detail="Это текстовая инструкция. Для нового разбора загрузите видео.")
    if g.status == "running":
        raise HTTPException(status_code=409, detail="Разбор уже идёт")
    g.status, g.message = "running", ""
    db.commit()
    tasks.add_task(analyze_guide, g.id)
    return _guide_out(g)


@router.delete("/guides/{guide_id}", status_code=204)
def delete_guide(guide_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    g = _get_guide(db, guide_id)
    if g.disk_name:
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


# Очереди проверки: у каждого поставщика своя и идёт в своём потоке, так что Армтек, Форум и другие работают параллельно.
_QUEUES: dict[int, dict] = ACTIVE_QUEUES


def _fresh() -> dict:
    return {"active": False, "ids": [], "pos": 0, "cancel": False, "done": [], "note": ""}


def _queue_out(db: Session, supplier_id: int | None = None) -> dict:
    for sid in db.scalars(select(ReturnJob.supplier_id).distinct()):
        load_queue(db, sid)
    if supplier_id is None:  # без указания поставщика: любая идущая очередь, иначе последняя
        pick = [k for k, q in _QUEUES.items() if q["active"]] or list(_QUEUES)
        supplier_id = pick[0] if pick else None
    if supplier_id is not None:
        load_queue(db, supplier_id)
    q = _QUEUES.get(supplier_id) if supplier_id is not None else None
    q = q or _fresh()
    cur = None
    if q["active"] and 0 < q["pos"] <= len(q["ids"]):
        b = db.get(ReturnBatch, q["ids"][q["pos"] - 1])
        cur = {"id": b.id, "number": b.number} if b else None
    active = [k for k, x in _QUEUES.items() if x["active"]]
    return {
        "supplier_id": supplier_id, "active": q["active"], "total": len(q["ids"]), "pos": q["pos"], "current": cur,
        "done": q["done"], "note": q["note"], "active_suppliers": active,
    }


def _run_queue(supplier_id: int, ids: list[int], user_id: int) -> None:
    """Обрабатывает документы одного поставщика по очереди. Остановка текущей проверки или «Отменить» прекращает эту очередь."""
    from ..db import SessionLocal

    q = _QUEUES[supplier_id]
    if q.get("job_id"):
        return run_job(q["job_id"], run_check)
    try:
        for i, bid in enumerate(ids, 1):
            if q["cancel"]:
                break
            q["pos"] = i
            with SessionLocal() as db:
                if q["cancel"]:
                    break
                mode = q.get("mode", "check")
                log = q.get("approvals", {}).get(bid, "[]")
                parts = split_approval(log, q.get("workers", 1)) if mode == "submit" else [log]
                runs = [ReturnRun(batch_id=bid, mode=mode, created_by=user_id, log=part) for part in parts]
                db.add_all(runs)
                db.commit()
                run_ids = [run.id for run in runs]
                if q["cancel"]:
                    for run in runs:
                        run.status = "stopped"
                    db.commit()
            try:
                execute_runs(run_ids, run_check)
            except Exception:  # noqa: BLE001
                pass
            with SessionLocal() as db:
                statuses = [db.get(ReturnRun, rid).status for rid in run_ids]
                status = "stopped" if "stopped" in statuses else "done" if all(s == "done" for s in statuses) else "failed"
                q["done"].append({"batch_id": bid, "status": status})
                if status == "stopped":
                    q["cancel"] = True
    finally:
        q["active"] = False


class QueueIn(BaseModel):
    batch_ids: list[int]
    mode: str = "check"
    confirmed: bool = False
    workers: int = Field(default=1, ge=1, le=5)


@router.post("/check-queue", status_code=202)
def start_queue(body: QueueIn, db: Session = Depends(get_db), me: User = Depends(current_user)):
    with RUN_LOCK:
        """Проверка нескольких документов подряд. Ничего не отправляет, как и одиночная проверка.

        Документы раскладываются по поставщикам: у каждого своя очередь в своём потоке, разные поставщики идут одновременно.
        """
        import threading

        _owner(me)
        if body.mode not in ("check", "submit"):
            raise HTTPException(status_code=422, detail="Неизвестный режим запуска")
        if body.mode == "submit" and not body.confirmed:
            raise HTTPException(status_code=422, detail="Подтвердите оформление возвратов")
        ids = list(dict.fromkeys(body.batch_ids))
        if len(ids) > 50:
            raise HTTPException(status_code=422, detail="Можно запустить до 50 сборок за один раз")
        approvals = {}
        if not ids:
            raise HTTPException(status_code=422, detail="Выберите хотя бы один документ")
        groups: dict[int, list[int]] = {}
        for bid in ids:
            batch = db.get(ReturnBatch, bid)
            if batch is None:
                raise HTTPException(status_code=404, detail=f"Документ {bid} не найден")
            sup = db.get(ReturnSupplier, batch.supplier_id) if batch.supplier_id else None
            if sup is None or not sup.portal_url:
                raise HTTPException(status_code=422, detail=f"У поставщика документа {batch.number or bid} не указан адрес кабинета")
            if not sup.session_encrypted:
                raise HTTPException(status_code=422, detail=f"Для документа {batch.number or bid} сначала войдите в кабинет и сохраните вход")
            if supplier_busy(db, sup.id):
                raise HTTPException(status_code=409, detail=f"У поставщика «{sup.name}» очередь уже идёт: дождитесь окончания или отмените её")
            if db.scalar(select(ReturnRun.id).join(ReturnBatch, ReturnBatch.id == ReturnRun.batch_id).where(ReturnRun.status == "running", ReturnBatch.supplier_id == sup.id)):
                raise HTTPException(status_code=409, detail=f"У поставщика «{sup.name}» проверка уже идёт")
            if body.mode == "submit":
                if not supplier_kind(sup.portal_url):
                    raise HTTPException(status_code=422, detail="Оформление поддерживается для Армтека и Форума")
                rows = eligible_rows(db, bid)
                approvals[bid] = approval_log(rows, sup.portal_url)
                for row in rows:
                    if row.status == "failed":
                        row.status, row.note = "pending", ""
            groups.setdefault(sup.id, []).append(bid)
        db.commit()
        for sid, part in groups.items():
            workers = body.workers if supplier_kind(db.get(ReturnSupplier, sid).portal_url) == "armtek" else 1
            _QUEUES[sid] = {**_fresh(), "active": True, "ids": part, "mode": body.mode, "approvals": approvals, "workers": workers}
            if body.mode == "submit":
                job = create_job(db, db.get(ReturnSupplier, sid), part, {bid: approvals[bid] for bid in part}, workers, me.id)
                _QUEUES[sid]["job_id"] = job.id
        db.commit()
        for sid, part in groups.items():
            threading.Thread(target=_run_queue, args=(sid, part, me.id), daemon=True, name=f"returns-queue-{sid}").start()
        return _queue_out(db, next(iter(groups)))


@router.get("/check-queue")
def queue_state(supplier_id: int | None = None, db: Session = Depends(get_db), me: User = Depends(current_user)):
    _owner(me)
    return _queue_out(db, supplier_id)


@router.post("/check-queue/cancel")
def cancel_queue(supplier_id: int | None = None, db: Session = Depends(get_db), me: User = Depends(current_user)):
    with RUN_LOCK:
        _owner(me)
        _queue_out(db, supplier_id)
        targets = [supplier_id] if supplier_id is not None else [k for k, q in _QUEUES.items() if q["active"]]
        for sid in targets:
            stop_jobs(db, sid)
            q = _QUEUES.get(sid)
            if not q:
                continue
            q["cancel"] = True
            if q["active"] and 0 < q["pos"] <= len(q["ids"]):
                for r in db.scalars(select(ReturnRun).where(ReturnRun.batch_id == q["ids"][q["pos"] - 1], ReturnRun.status == "running")):
                    r.status = "stopped"
        db.commit()
        return _queue_out(db, supplier_id)


def _run_out(r: ReturnRun) -> dict:
    entries = json.loads(r.log or "[]")
    worker = json.loads(entries[0][len(APPROVAL_PREFIX):]).get("worker", {}) if entries and entries[0].startswith(APPROVAL_PREFIX) else {}
    return {
        "id": r.id, "batch_id": r.batch_id, "mode": r.mode, "status": r.status, "summary": r.summary,
        "has_shot": bool(r.last_shot), "log": [entry for entry in json.loads(r.log or "[]") if not entry.startswith(APPROVAL_PREFIX)], "created_at": r.created_at.isoformat(),
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "worker_index": worker.get("index", 1), "worker_total": worker.get("total", 1),
    }


@router.post("/batches/{batch_id}/check-run", status_code=202)
def start_check(batch_id: int, tasks: BackgroundTasks, db: Session = Depends(get_db), me: User = Depends(current_user)):
    with RUN_LOCK:
        """Проверка счетов на сайте поставщика под сохранённым входом. Ничего не отправляет."""
        _owner(me)
        batch = db.get(ReturnBatch, batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="Документ не найден")
        sup = db.get(ReturnSupplier, batch.supplier_id) if batch.supplier_id else None
        if sup is None or not sup.portal_url:
            raise HTTPException(status_code=422, detail="У поставщика не указан адрес кабинета")
        if not sup.session_encrypted:
            raise HTTPException(status_code=422, detail="Сначала войдите в кабинет и сохраните вход")
        busy = db.scalar(
            select(ReturnRun.id).join(ReturnBatch, ReturnBatch.id == ReturnRun.batch_id).where(ReturnRun.status == "running", ReturnBatch.supplier_id == sup.id)
        )
        if busy or supplier_busy(db, sup.id):
            raise HTTPException(status_code=409, detail="У этого поставщика проверка уже идёт")
        run = ReturnRun(batch_id=batch.id, mode="check", created_by=me.id)
        db.add(run)
        db.commit()
        tasks.add_task(run_check, run.id)
        return _run_out(run)


class SubmitIn(BaseModel):
    confirmed: bool
    workers: int = Field(default=1, ge=1, le=5)


@router.post("/batches/{batch_id}/submit-run", status_code=202)
def start_submit(batch_id: int, body: SubmitIn, tasks: BackgroundTasks, db: Session = Depends(get_db), me: User = Depends(current_user)):
    with RUN_LOCK:
        _owner(me)
        if not body.confirmed:
            raise HTTPException(status_code=422, detail="Подтвердите оформление возвратов")
        batch = db.get(ReturnBatch, batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="Сборка не найдена")
        sup = _get_supplier(db, batch.supplier_id)
        if not sup.portal_url or not sup.session_encrypted:
            raise HTTPException(status_code=422, detail="Укажите кабинет поставщика и сохраните вход")
        if not supplier_kind(sup.portal_url):
            raise HTTPException(status_code=422, detail="Оформление поддерживается для Армтека и Форума")
        if supplier_busy(db, sup.id):
            raise HTTPException(status_code=409, detail="У поставщика уже идёт запуск или очередь")
        rows = eligible_rows(db, batch.id)
        if not rows:
            raise HTTPException(status_code=409, detail="Нет доступных к оформлению строк. Отправленные и спорные отправки защищены.")
        log = approval_log(rows, sup.portal_url)
        for row in rows:
            if row.status == "failed":
                row.status, row.note = "pending", ""
        workers = body.workers if supplier_kind(sup.portal_url) == "armtek" else 1
        runs = [ReturnRun(batch_id=batch.id, mode="submit", created_by=me.id, log=part) for part in split_approval(log, workers)]
        db.add_all(runs)
        db.flush()
        job = create_job(db, sup, [batch.id], {batch.id: log}, workers, me.id, [run.id for run in runs])
        db.commit()
        tasks.add_task(run_job, job.id, run_check)
        return _run_out(runs[0])


@router.get("/suppliers/{supplier_id}/runs")
def supplier_runs(supplier_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    """Последние запуски по всем документам поставщика (журнал не теряется при смене документа)."""
    _get_supplier(db, supplier_id)
    q = (
        select(ReturnRun).join(ReturnBatch, ReturnBatch.id == ReturnRun.batch_id)
        .where(ReturnBatch.supplier_id == supplier_id).order_by(ReturnRun.id.desc()).limit(10)
    )
    return [_run_out(r) for r in db.scalars(q)]


@router.get("/batches/{batch_id}/runs")
def list_runs(batch_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    if db.get(ReturnBatch, batch_id) is None:
        raise HTTPException(status_code=404, detail="Документ не найден")
    return [_run_out(r) for r in db.scalars(select(ReturnRun).where(ReturnRun.batch_id == batch_id).order_by(ReturnRun.id.desc()).limit(10))]


@router.get("/runs/{run_id}/shot")
def run_shot(run_id: int, db: Session = Depends(get_db), me: User = Depends(current_user)):
    """Последний скриншот окна агента: что он видел, когда закончил."""
    _owner(me)
    r = db.get(ReturnRun, run_id)
    if r is None or not r.last_shot:
        raise HTTPException(status_code=404, detail="Скриншота нет")
    import base64

    return Response(content=base64.b64decode(r.last_shot), media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.post("/runs/{run_id}/stop")
def stop_run(run_id: int, db: Session = Depends(get_db), me: User = Depends(current_user)):
    with RUN_LOCK:
        _owner(me)
        r = db.get(ReturnRun, run_id)
        if r is None:
            raise HTTPException(status_code=404, detail="Запуск не найден")
        batch = db.get(ReturnBatch, r.batch_id)
        if batch and r.status == "running":
            stop_jobs(db, batch.supplier_id)
        if r.status == "running":
            r.status = "stopped"
        db.commit()
        return _run_out(r)


def _assembly_file(db: Session, file_id: int):
    from ..models import Folder, StoredFile

    f = db.get(StoredFile, file_id)
    folder = db.get(Folder, f.folder_id) if f else None
    if f is None or folder is None or not folder.name.startswith("Сборка 1С — "):
        raise HTTPException(status_code=404, detail="Это не файл сборки из 1С")
    path = Path(settings.files_dir).resolve() / f.disk_name
    if not path.is_file():
        raise HTTPException(status_code=410, detail="Файл потерян на сервере")
    return f, folder.name.removeprefix("Сборка 1С — "), path.read_text(encoding="utf-8", errors="replace")


@router.get("/from-file/{file_id}")
def batch_of_file(file_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    """Есть ли уже документ возврата по этому файлу сборки."""
    b = ar.batch_for_file(db, file_id)
    return {"batch_id": b.id, "supplier_id": b.supplier_id} if b else {"batch_id": None, "supplier_id": None}


@router.post("/from-file/{file_id}", status_code=201)
def make_from_file(file_id: int, db: Session = Depends(get_db), me: User = Depends(current_user)):
    """Документ возврата по сохранённой сборке 1С (для файлов, сохранённых до автоматического создания, или после правки разбора)."""
    f, supplier, text = _assembly_file(db, file_id)
    try:
        batch = ar.batch_from_file(db, f, supplier, text, me.id)
    except ar.Locked as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Не получилось разобрать таблицу: {exc}") from exc
    return _summary(db, batch)
