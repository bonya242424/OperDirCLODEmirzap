"""Область и проверка полноты сборки, сохраняемые между продолжениями."""

import json
import re
from datetime import datetime

from sqlalchemy import select

from .browser import BrowserError
from .models import AssemblyQuestion, ReturnSupplier


def number(value):
    return str(value).strip().lstrip("0")


def scope(db, run):
    data = json.loads(run.inventory or "{}")
    if not data:
        data = {"root_id": run.id, "numbers": [], "skipped": {}, "complete": False,
                "suppliers": list(db.scalars(select(ReturnSupplier.name).where(ReturnSupplier.is_active.is_(True))))}
        # Явная дата поручения важнее начального значения календаря интерфейса.
        match = re.search(r"(?:за|только\s+за|период)\s+(\d{2}\.\d{2}\.\d{4})(?:\s*[–—-]\s*(\d{2}\.\d{2}\.\d{4}))?", run.note or "", re.I)
        if match:
            try:
                start = datetime.strptime(match[1], "%d.%m.%Y").date()
                end = datetime.strptime(match[2] or match[1], "%d.%m.%Y").date()
                if start <= end:
                    run.date_from, run.date_to = start, end
            except ValueError:
                pass
        store(db, run, data)
    return data


def store(db, run, data):
    run.inventory = json.dumps(data, ensure_ascii=False)
    db.commit()


def register(db, run, numbers, all_suppliers=False, supplier="", source=""):
    data = scope(db, run)
    data["numbers"] = list(dict.fromkeys(data["numbers"] + [number(n) for n in numbers if number(n)]))
    scanned = set(data.get("scanned_suppliers", []))
    targets = data.get("suppliers", [])
    if supplier:
        matched = next((name for name in targets if name.casefold() == supplier.strip().casefold()), None)
        if not matched:
            raise BrowserError("Поставщик не входит в сохранённое задание")
        scanned.add(matched)
    if all_suppliers:
        scanned.update(name for name in targets if name.casefold() in source.casefold())
    data["scanned_suppliers"] = sorted(scanned)
    data["complete"] = data.get("complete", False) or bool((all_suppliers or supplier) and set(targets).issubset(scanned))
    store(db, run, data)


def saved_numbers(db):
    from .assembly_run import doc_number
    from .models import Folder, StoredFile
    return {doc_number(f.name.split(", сохранено")[0]) for f in db.scalars(
        select(StoredFile).join(Folder, Folder.id == StoredFile.folder_id).where(Folder.name.like("Сборка 1С — %")))}


def progress(db, run):
    data = scope(db, run)
    have = saved_numbers(db)
    left = [n for n in data["numbers"] if n not in have and n not in data["skipped"]]
    errors = [n for n, item in data["skipped"].items() if item["kind"] == "error"]
    return data.get("complete", False), left, errors


def skip(db, run, num, kind, reason):
    data = scope(db, run)
    num = number(num)
    if num not in data["numbers"] or kind not in ("unposted", "outside_scope", "error") or not reason.strip():
        raise BrowserError("Укажите номер из перечня, тип пропуска и конкретную причину")
    if num in saved_numbers(db):
        raise BrowserError("Документ уже сохранён; пропуск не нужен")
    data["skipped"][num] = {"kind": kind, "reason": reason[:500]}
    store(db, run, data)
    return f"Пропуск №{num} записан: {reason}"


def clarifications(db, run):
    return "\n".join(f"Вопрос: {q.question}\nОтвет владельца: {q.answer}" for q in db.scalars(
        select(AssemblyQuestion).where(AssemblyQuestion.run_id == run.id, AssemblyQuestion.answer.is_not(None)).order_by(AssemblyQuestion.id)))
