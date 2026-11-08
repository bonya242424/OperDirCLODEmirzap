"""Доступы поставщиков: серверный импорт только Лист12, без передачи секретов модели."""
import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
from sqlalchemy import select

from . import connections
from .browser import BrowserError
from .db import SessionLocal
from .models import AppSetting, ReturnSupplier, ReturnBatch, ReturnJob, ReturnLine, User, utcnow

log = logging.getLogger(__name__)
# Источник и вкладка явно выбраны владельцем в поручении 08.11.2026.
SHEET_ID = "1rn7bHBzugP-tu0zv4rQeDHOgVa4xZP_I"
SHEET_NAME = "Лист12"
STATUS_KEY = "supplier-credentials-sheet12-status"
APPROVAL_KEY = "supplier-returns-owner-approval-20261108"
_LOCKS = {}
_LOCKS_LOCK = threading.Lock()
_IMPORT_LOCK = threading.Lock()

# Названия из таблицы сопоставляются с существующими карточками, без дублей.
SUPPLIERS = {
    "favorit-parts.ru": ("фаворит возвраты (1)", ("фаворит", "favorit")),
    "armtek.ru": ("Армтек", ("армтек", "armtek")),
    "shate-m.ru": ("Шате", ("шате", "shate")),
    "forum-auto.ru": ("Форум авто", ("форум", "forum")),
    "berg.ru": ("Берг", ("берг", "berg")),
    "rossko.ru": ("Росско", ("росско", "rossko")),
    "autokontinent.ru": ("Автоконтинент", ("автоконтинент", "autokontinent")),
    "moskvorechie.ru": ("москворечье возвраты (1)", ("москвореч",)),
    "ixora-auto.ru": ("ИКСОРА", ("иксора", "ixora")),
    "part-kom.ru": ("Партком", ("партком", "partkom")),
    "motexc.ru": ("мотех", ("мотех", "мотекс", "motex")),
}


class CredentialsError(Exception):
    """Только фиксированный текст, без значений таблицы."""


def canonical_login_url(raw):
    value = str(raw).strip()
    if "://" not in value:
        value = "https://" + value
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or parsed.username or parsed.password or parsed.port not in (None, 80, 443):
        raise CredentialsError("Недопустимый адрес входа")
    host = (parsed.hostname or "").lower()
    if not any(host == domain or host.endswith("." + domain) for domain in SUPPLIERS):
        raise CredentialsError("Адрес входа не относится к поставщикам задания")
    return urlunsplit(("https", host, parsed.path or "/", parsed.query, ""))


def decode_sheet(raw):
    """headers=1 обязателен: автоопределение GViz способно включить секреты в labels."""
    try:
        data = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
        if data.get("status") != "ok":
            raise ValueError()
        table = data["table"]
        headers = [str(col.get("label", "")).strip().casefold() for col in table["cols"]]
        if headers != ["поставщик", "адрес", "логин", "пароль"]:
            raise ValueError()
        result = []
        for record in table.get("rows", []):
            cells = record.get("c", [])
            values = []
            for index in range(4):
                value = cells[index].get("v") if index < len(cells) and cells[index] else ""
                if value is None:
                    value = ""
                if isinstance(value, float) and value.is_integer():
                    value = int(value)
                values.append(str(value))
            if not any(values):
                continue
            name, address, username, password = values
            result.append((name.strip(), address, username.strip(), password))
        return result
    except (ValueError, TypeError, KeyError, IndexError):
        raise CredentialsError("Лист12 недоступен или его столбцы отличаются от Поставщик / Адрес / Логин / Пароль") from None


def fetch_sheet():
    query = urlencode({"sheet": SHEET_NAME, "headers": "1", "range": "A1:D500", "tq": "select A,B,C,D", "tqx": "out:json"})
    try:
        response = httpx.get(f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?{query}",
                             timeout=20, follow_redirects=False)
        if response.status_code != 200 or len(response.content) > 1_000_000:
            raise CredentialsError("Не удалось прочитать Лист12")
        return decode_sheet(response.text)
    except httpx.HTTPError:
        raise CredentialsError("Google Таблица временно недоступна") from None


def set_setting(db, key, value):
    row = db.get(AppSetting, key)
    if row:
        row.value = json.dumps(value, ensure_ascii=False)
    else:
        db.add(AppSetting(key=key, value=json.dumps(value, ensure_ascii=False)))


def import_rows(db, rows):
    imported, skipped, ids = [], [], []
    existing = list(db.scalars(select(ReturnSupplier)))
    for name, address, username, password in rows:
        if not name or not username or not password:
            skipped.append({"supplier": name, "reason": "Не заполнены адрес, логин или пароль"})
            continue
        try:
            url = canonical_login_url(address)
        except (CredentialsError, ValueError):
            skipped.append({"supplier": name, "reason": "Нет допустимого адреса входа"})
            continue
        host = urlsplit(url).hostname
        domain = next(d for d in SUPPLIERS if host == d or host.endswith("." + d))
        canonical, aliases = SUPPLIERS[domain]
        # Совпадения названия и домена проверяются до использования секретов.
        if not any(alias in name.casefold() for alias in aliases):
            skipped.append({"supplier": name, "reason": "Название и адрес поставщика не соответствуют друг другу"})
            continue
        candidates = [s for s in existing if s.name.casefold() == canonical.casefold()
                      or any(alias in s.name.casefold() for alias in aliases)
                      or (urlsplit(s.portal_url).hostname or "").endswith(domain)]
        if len(candidates) > 1:
            exact = [s for s in candidates if s.name.casefold() == canonical.casefold()]
            candidates = exact if len(exact) == 1 else candidates
        if len(candidates) > 1:
            skipped.append({"supplier": name, "reason": "Несколько карточек поставщика; доступы не заменены"})
            continue
        supplier = candidates[0] if candidates else ReturnSupplier(name=canonical)
        if not candidates:
            db.add(supplier)
            existing.append(supplier)
        if not supplier.is_active and candidates:
            skipped.append({"supplier": name, "reason": "Карточка выключена владельцем"})
            continue
        current_host = (urlsplit(supplier.portal_url or "").hostname or "").lower()
        if current_host and not (current_host == domain or current_host.endswith("." + domain)):
            skipped.append({"supplier": name, "reason": "Адрес действующей карточки отличается от адреса таблицы"})
            continue
        config = {"username": username, "password": password}
        changed = connections.decrypt(supplier.credentials_encrypted or "") != config or supplier.login_url != url
        supplier.credentials_encrypted = connections.encrypt(config) if changed else supplier.credentials_encrypted
        supplier.login_url = url
        if not supplier.portal_url:
            supplier.portal_url = url
        if changed or supplier.login_status == "error":
            supplier.login_status, supplier.login_error = "ready", ""
        db.flush()
        ids.append(supplier.id)
        imported.append(supplier.name)
    return {"sheet": SHEET_NAME, "imported": imported, "skipped": skipped, "supplier_ids": ids, "at": utcnow().isoformat()}


def supplier_lock(supplier_id):
    with _LOCKS_LOCK:
        return _LOCKS.setdefault(supplier_id, threading.Lock())


def ensure_session(db, supplier, manager, key, force=False, verify_credentials=False):
    """Секреты передаются только детерминированному драйверу, никогда в инструменты ИИ."""
    if not supplier.credentials_encrypted:
        raise BrowserError("Нужен вход в кабинет поставщика")
    with supplier_lock(supplier.id):
        db.refresh(supplier)
        authenticated = lambda: manager.session_authenticated(key) if hasattr(manager, "session_authenticated") else not manager.auth_required(key)
        if not force and supplier.session_encrypted and authenticated():
            return False
        config = connections.decrypt(supplier.credentials_encrypted)
        if not config.get("username") or not config.get("password"):
            raise BrowserError("Нужен вход: доступы поставщика не настроены")
        if supplier.login_status == "error":
            raise BrowserError("Нужен вход: предыдущий автовход не подтверждён; проверьте доступы в Лист12")
        try:
            # Другой поток мог уже обновить общую сохранённую сессию.
            raw = connections.decrypt(supplier.session_encrypted).get("state")
            if raw and not verify_credentials:
                manager.open(key, supplier.portal_url, json.loads(raw))
                if authenticated():
                    return False
            state = manager.login(key, supplier.login_url, config["username"], config["password"], supplier.portal_url)
            supplier.session_encrypted = connections.encrypt({"state": json.dumps(state)})
            supplier.session_saved_at = utcnow()
            supplier.login_status, supplier.login_error = "ok", ""
            db.commit()
            return True
        except BrowserError:
            if hasattr(manager, "login_diagnostic"):
                try:
                    set_setting(db, f"supplier-login-diagnostic:{supplier.id}", manager.login_diagnostic(key))
                except BrowserError:
                    pass
            supplier.login_status, supplier.login_error = "error", "Автовход не подтверждён. Проверьте адрес и доступы в Лист12."
            db.commit()
            raise BrowserError("Нужен вход: автоматическая авторизация не подтверждена; очередь сохранена") from None


def capture_owner_approval(db, supplier_ids):
    """Владелец явно разрешил запуск 08.11.2026. Один неизменяемый снимок существующих строк."""
    from .return_submission import eligible_rows, approval_log
    if db.get(AppSetting, APPROVAL_KEY):
        return
    owner = db.scalar(select(User).where(User.role == "owner").order_by(User.id))
    if not owner:
        return
    groups = {}
    for supplier in db.scalars(select(ReturnSupplier).where(ReturnSupplier.id.in_(supplier_ids))):
        batches, approvals = [], {}
        for batch in db.scalars(select(ReturnBatch).where(ReturnBatch.supplier_id == supplier.id).order_by(ReturnBatch.doc_date, ReturnBatch.id)):
            rows = eligible_rows(db, batch.id)
            if rows:
                batches.append(batch.id)
                approvals[str(batch.id)] = approval_log(rows, supplier.portal_url)
        groups[str(supplier.id)] = {"portal_url": supplier.portal_url, "batch_ids": batches, "approvals": approvals}
    set_setting(db, APPROVAL_KEY, {"owner_id": owner.id, "at": utcnow().isoformat(), "suppliers": groups, "started": {}})
    db.commit()


def launch_approved(db):
    from .return_submission import RUN_LOCK, supplier_kind, supplier_busy, approval_log, read_approval
    from .return_jobs import create_job
    marker = db.get(AppSetting, APPROVAL_KEY)
    if not marker:
        return
    with RUN_LOCK:
        data = json.loads(marker.value)
        for sid, group in data["suppliers"].items():
            if sid in data["started"]:
                continue
            supplier = db.get(ReturnSupplier, int(sid))
            if not supplier or not supplier.is_active or supplier.portal_url != group["portal_url"]:
                continue
            if not supplier_kind(supplier.portal_url) or supplier.login_status != "ok":
                continue
            if supplier_busy(db, supplier.id):
                continue
            ids, approvals = [], {}
            for bid in group["batch_ids"]:
                # Снимок не расширяется новыми строками/сборками после разрешения.
                saved = json.loads(group["approvals"][str(bid)])[0]
                from .return_submission import APPROVAL_PREFIX
                expected = {item["id"]: item for item in json.loads(saved[len(APPROVAL_PREFIX):])["lines"]}
                rows = [row for row in db.scalars(select(ReturnLine).where(ReturnLine.batch_id == bid,
                        ReturnLine.status.in_(("pending", "checked", "failed")), ReturnLine.request_no == ""))
                        if expected.get(row.id) == {key: getattr(row, key) for key in ("id", "article", "brand", "qty", "invoice_no")}]
                from .assembly_scope import source_block_reason
                batch = db.get(ReturnBatch, bid)
                if not batch or source_block_reason(db, batch):
                    continue
                if rows:
                    for row in rows:
                        if row.status == "failed":
                            row.status, row.note = "pending", ""
                    ids.append(bid)
                    approvals[bid] = approval_log(rows, supplier.portal_url)
            if ids:
                job = create_job(db, supplier, ids, approvals, 1, data["owner_id"])
                data["started"][sid] = job.id
            else:
                data["started"][sid] = "no_pending_rows"
        marker.value = json.dumps(data, ensure_ascii=False)
        db.commit()


def preflight_one(supplier_id):
    from .browser import manager
    key = 3_000_000 + supplier_id
    try:
        with SessionLocal() as db:
            supplier = db.get(ReturnSupplier, supplier_id)
            if not supplier or not supplier.credentials_encrypted or supplier.login_status == "error":
                return
            if supplier.login_status == "ok" and supplier.session_encrypted:
                launch_approved(db)
                return
            # Проверяем именно доступы таблицы в чистом контексте, не принимая старую cookie за проверку пароля.
            manager.open(key, supplier.portal_url, None)
            ensure_session(db, supplier, manager, key, force=True, verify_credentials=True)
            db.refresh(supplier)
            supplier.login_status, supplier.login_error = "ok", ""
            db.commit()
            launch_approved(db)
    except BrowserError:
        with SessionLocal() as db:
            supplier = db.get(ReturnSupplier, supplier_id)
            if supplier:
                supplier.login_status, supplier.login_error = "error", "Автоматический вход не подтверждён; проверьте доступы и форму входа."
                db.commit()
        log.warning("Автовход поставщика %s не подтверждён; секреты не записывались в журнал", supplier_id)
    finally:
        try:
            manager.close(key)
        except BrowserError:
            pass


def synchronize():
    """Импорт и проверка входа в фоне. Повторный запуск не создаёт повторных заявок."""
    if not _IMPORT_LOCK.acquire(blocking=False):
        return
    try:
        rows = fetch_sheet()
        with SessionLocal() as db:
            status = import_rows(db, rows)
            set_setting(db, STATUS_KEY, status)
            db.commit()
            capture_owner_approval(db, status["supplier_ids"])
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="supplier-login") as pool:
            list(pool.map(preflight_one, status["supplier_ids"]))
    except CredentialsError as exc:
        log.warning("Импорт доступов: %s", exc)
    finally:
        _IMPORT_LOCK.release()


def sanitized_status():
    from .return_submission import supplier_kind
    with SessionLocal() as db:
        status = db.get(AppSetting, STATUS_KEY)
        imported = json.loads(status.value) if status else {}
        ids = imported.get("supplier_ids", [])
        result = []
        for supplier in db.scalars(select(ReturnSupplier).where(ReturnSupplier.id.in_(ids))):
            job = db.scalar(select(ReturnJob).where(ReturnJob.supplier_id == supplier.id).order_by(ReturnJob.id.desc()))
            diagnostic = db.get(AppSetting, f"supplier-login-diagnostic:{supplier.id}")
            result.append({"supplier": supplier.name, "login": supplier.login_status,
                           "returns": job.status if job else ("not_started" if supplier_kind(supplier.portal_url) else "adapter_required"),
                           "diagnostic": json.loads(diagnostic.value) if diagnostic and supplier.login_status == "error" else None})
        return {"imported": len(ids), "skipped": len(imported.get("skipped", [])), "suppliers": result}


if __name__ == "__main__":
    import sys
    import time
    if "--wait-status" in sys.argv:
        for _ in range(80):
            report = sanitized_status()
            if report["imported"] and all(item["login"] in ("ok", "error") for item in report["suppliers"]):
                break
            time.sleep(3)
        print(json.dumps(sanitized_status(), ensure_ascii=False))
    else:
        print(json.dumps(sanitized_status(), ensure_ascii=False))
