"""Документ возврата из сохранённой сборки 1С: печатная форма разбирается в строки возврата у поставщика.

Разбирает код, модель в этом не участвует: что в таблице, то и в документе. Шапку таблицы ищем по названиям колонок,
потому что печатные формы 1С отличаются у разных поставщиков (номер строки, лишние строки сверху, «Итого» снизу).
"""

import csv
import io
import re
from datetime import date

from sqlalchemy import select

from .return_submission import LOCKED_STATUSES, RUN_LOCK, supplier_busy
from .models import ReturnBatch, ReturnLine, ReturnRun, ReturnSupplier, StoredFile, utcnow

MAX_LINES = 2000

# В каком порядке искать колонку: первое совпадение по шапке побеждает.
ARTICLE = ("артикул", "код товара", "код", "товар", "номенклатура")
NAME = ("наименование", "номенклатура", "товар", "наименование товара")
BRAND = ("производитель", "бренд", "марка")
WAREHOUSE = ("начальный склад возврата", "склад")
INVOICE_NO = ("номер счета", "номер счёта", "счет", "счёт", "счет поставщика", "счёт поставщика", "номер счета поставщика")
INVOICE_DATE = ("дата счета", "дата счёта")
REASON = ("причина возврата", "причина")
DATE_RE = re.compile(r"от\s+(\d{1,2})\.(\d{1,2})\.(\d{4})")


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip().lower().rstrip(".:")


def doc_date_of(number: str) -> date | None:
    m = DATE_RE.search(number or "")
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def _rows(text: str) -> list[list[str]]:
    text = text.lstrip("﻿").replace("\r\n", "\n")
    sample = text[:5000]
    delim = "\t" if sample.count("\t") > sample.count(";") else (";" if ";" in sample else ",")
    return [[c.strip() for c in r] for r in csv.reader(io.StringIO(text), delimiter=delim)]


def _find(header: list[str], names: tuple[str, ...], taken: set[int]) -> int | None:
    cells = [norm(c) for c in header]
    for name in names:
        for j, c in enumerate(cells):
            if j not in taken and c == name:
                return j
    return None


def _qty(raw: str) -> int | None:
    s = re.sub(r"[\s\xa0]", "", raw or "").replace(",", ".")
    try:
        v = float(s)
    except ValueError:
        return None
    return int(round(v)) if v > 0 else None


def parse_print_form(text: str) -> list[dict]:
    """Строки товаров из печатной формы. ValueError с понятным текстом, если таблицу не узнали."""
    rows = _rows(text)
    header_at, cols = -1, {}
    for i, r in enumerate(rows[:60]):
        cells = [norm(c) for c in r]
        qty = next((j for j, c in enumerate(cells) if c.startswith("кол")), None)
        if qty is None:
            continue
        taken = {qty}
        art = _find(r, ARTICLE, taken)
        if art is None:
            continue
        taken.add(art)
        cols = {"qty": qty, "article": art}
        for key, names in (("name", NAME), ("brand", BRAND), ("warehouse", WAREHOUSE), ("invoice_no", INVOICE_NO), ("invoice_date", INVOICE_DATE), ("reason", REASON)):
            j = _find(r, names, taken)
            if j is not None:
                cols[key] = j
                taken.add(j)
        header_at = i
        break
    if header_at < 0:
        raise ValueError("В таблице не нашлась шапка с колонками «Артикул» (или «Код», «Товар») и «Количество»")
    lines: list[dict] = []
    for r in rows[header_at + 1 :]:
        def get(key: str) -> str:
            j = cols.get(key)
            return r[j].strip() if j is not None and j < len(r) else ""

        if r and norm(r[0]).startswith("итог"):
            break
        article, qty = get("article"), _qty(get("qty"))
        if not article or qty is None or norm(article).startswith("итог"):
            continue  # пустые строки, подытоги, строки без количества
        lines.append({
            "article": article[:100], "name": get("name")[:500], "brand": get("brand")[:200], "qty": qty,
            "warehouse": get("warehouse")[:200], "invoice_date": get("invoice_date")[:32], "invoice_no": get("invoice_no")[:100], "reason": get("reason")[:300],
        })
        if len(lines) > MAX_LINES:
            raise ValueError(f"Слишком много строк (больше {MAX_LINES})")
    if not lines:
        raise ValueError("В таблице не нашлось строк с товаром и количеством")
    return lines


def supplier_by_name(db, name: str, user_id: int | None) -> ReturnSupplier:
    """Поставщик системы с этим названием (без учёта регистра), иначе новый."""
    clean = name.strip()[:200]
    for sup in db.scalars(select(ReturnSupplier)):
        if norm(sup.name) == norm(clean):
            return sup
    sup = ReturnSupplier(name=clean, created_by=user_id)
    db.add(sup)
    db.flush()
    return sup


def batch_key(number: str) -> str:
    """Один и тот же документ при разной записи («Сборка №000007007 от 02.11.2026» и «№000007007 от 02.11.2026»): номер без нулей и дата."""
    m = re.search(r"\d{4,}", number or "")
    d = doc_date_of(number)
    return f"{m.group(0).lstrip('0') or '0'}|{d.isoformat() if d else ''}" if m else norm(number)


class Locked(Exception):
    """Документ уже есть, и по нему отправлены заявки: заменять нельзя."""


def store_batch(db, sup: ReturnSupplier, number: str, lines: list[dict], source: str, user_id: int | None) -> ReturnBatch:
    with RUN_LOCK:
        """Документ возврата у поставщика. Тот же номер загружен повторно: старый заменяется, если по нему ничего не отправлено."""
        if supplier_busy(db, sup.id):
            raise Locked("У поставщика идёт запуск или очередь. Дождитесь завершения перед повторной загрузкой.")
        number = number.strip()[:200]
        if number:
            key = batch_key(number)
            for old in db.scalars(select(ReturnBatch).where(ReturnBatch.supplier_id == sup.id)).all():
                if batch_key(old.number) != key:
                    continue
                if db.scalar(select(ReturnLine.id).where(ReturnLine.batch_id == old.id, (ReturnLine.status.in_(LOCKED_STATUSES)) | (ReturnLine.request_no != "")).limit(1)):
                    raise Locked(f"Документ {number} уже загружен, и по нему есть отправленные заявки: заменить его нельзя")
                for r in db.scalars(select(ReturnRun).where(ReturnRun.batch_id == old.id)):
                    db.delete(r)
                for r in db.scalars(select(ReturnLine).where(ReturnLine.batch_id == old.id)):
                    db.delete(r)
                db.delete(old)
            db.flush()
        batch = ReturnBatch(supplier=sup.name, supplier_id=sup.id, number=number, source=source[:500], created_by=user_id, doc_date=doc_date_of(number) or utcnow().date())
        db.add(batch)
        db.flush()
        for ln in lines:
            row = {k: (v[:500] if isinstance(v, str) else v) for k, v in ln.items()}
            reason = (row.get("reason") or "").strip()
            if not reason or "не указ" in reason.lower():
                reason = (sup.default_reason or "").strip()
            if not reason or "не указ" in reason.lower():
                reason = "Отказ клиента"
            row["reason"] = reason
            db.add(ReturnLine(batch_id=batch.id, **row))
        db.commit()
        return batch


def label_of(file_name: str) -> str:
    """«Сборка 000006738 от 04.10.2026, сохранено …csv» → «000006738 от 04.10.2026»."""
    head = file_name.split(", сохранено")[0]
    head = re.sub(r"\.csv$", "", head, flags=re.I)
    return re.sub(r"^Сборка\s+", "", head).strip()


def source_of(f: StoredFile) -> str:
    return f"Сборка 1С, файл #{f.id}: {f.name}"


def batch_from_file(db, f: StoredFile, supplier: str, text: str, user_id: int | None) -> ReturnBatch:
    """Создаёт (или обновляет) документ возврата по сохранённому файлу сборки. ValueError/Locked с понятным текстом."""
    lines = parse_print_form(text)
    sup = supplier_by_name(db, supplier, user_id)
    return store_batch(db, sup, label_of(f.name), lines, source_of(f), user_id)


def batch_for_file(db, file_id: int) -> ReturnBatch | None:
    return db.scalar(select(ReturnBatch).where(ReturnBatch.source.like(f"Сборка 1С, файл #{file_id}:%")))


def dedupe(db) -> dict:
    with RUN_LOCK:
        """Убирает дубли документов возврата (один поставщик и один документ). Остаётся тот, где больше результатов проверки и отправлено больше всего,
        при равенстве тот, что создан из файла сборки 1С, затем самый новый. Удаляются только дубли без проверенных и отправленных строк."""
        from collections import defaultdict

        groups: dict[tuple, list[ReturnBatch]] = defaultdict(list)
        for b in db.scalars(select(ReturnBatch).where(ReturnBatch.number != "")):
            groups[(b.supplier_id or b.supplier, batch_key(b.number))].append(b)
        removed, skipped = 0, 0
        for batches in groups.values():
            if len(batches) < 2:
                continue

            def progress(b: ReturnBatch) -> tuple:
                sts = [x for x in db.scalars(select(ReturnLine.status).where(ReturnLine.batch_id == b.id))]
                return (sum(1 for x in sts if x == "submitted"), sum(1 for x in sts if x != "pending"), b.source.startswith("Сборка 1С, файл #"), b.id)

            best = max(batches, key=progress)
            for b in batches:
                if b.id == best.id:
                    continue
                if supplier_busy(db, b.supplier_id) or db.scalar(select(ReturnLine.id).where(ReturnLine.batch_id == b.id, ReturnLine.request_no != "").limit(1)) or progress(b)[1] > 0:  # по нему уже есть результаты: сами не удаляем
                    skipped += 1
                    continue
                for r in db.scalars(select(ReturnRun).where(ReturnRun.batch_id == b.id)):
                    db.delete(r)
                for r in db.scalars(select(ReturnLine).where(ReturnLine.batch_id == b.id)):
                    db.delete(r)
                db.delete(b)
                removed += 1
        db.commit()
        return {"removed": removed, "skipped": skipped}
