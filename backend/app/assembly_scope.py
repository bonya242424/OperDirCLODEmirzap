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
    if num not in data["numbers"] or kind not in ("unposted", "deleted", "outside_scope", "error") or not reason.strip():
        raise BrowserError("Укажите номер из перечня, тип пропуска и конкретную причину")
    if num in saved_numbers(db):
        raise BrowserError("Документ уже сохранён; пропуск не нужен")
    data["skipped"][num] = {"kind": kind, "reason": reason[:500]}
    store(db, run, data)
    return f"Пропуск №{num} записан: {reason}"


def clarifications(db, run):
    return "\n".join(f"Вопрос: {q.question}\nОтвет владельца: {q.answer}" for q in db.scalars(
        select(AssemblyQuestion).where(AssemblyQuestion.run_id == run.id, AssemblyQuestion.answer.is_not(None)).order_by(AssemblyQuestion.id)))


def document_key(supplier, label):
    from .assembly_returns import doc_date_of
    match = re.search(r"№\s*(\d+)", label) or re.search(r"\d{3,}", label)
    if not match:
        raise BrowserError("Не указан номер документа для проверки статуса")
    num = number(match[1] if match.lastindex else match[0])
    day = doc_date_of(label)
    name = re.sub(r"[^\w]", "", supplier.casefold())
    return f"{name}|{num}|{day.isoformat() if day else ''}"


def block_source(db, supplier, label, reason):
    from .models import AppSetting, ReturnBatch
    key = "assembly-source-block:" + document_key(supplier, label)
    setting = db.get(AppSetting, key)
    if setting is None:
        db.add(AppSetting(key=key, value=reason[:500]))
    else:
        setting.value = reason[:500]
    for batch in db.scalars(select(ReturnBatch)):
        try:
            same = document_key(batch.supplier, batch.number) == document_key(supplier, label)
        except BrowserError:
            continue
        if same:
            marker = "ИСКЛЮЧЁН: " + reason
            if marker not in batch.source:
                batch.source = (batch.source[:max(0, 497-len(marker))] + " · " + marker)[:500]


def source_block_reason(db, batch):
    from .models import AppSetting
    try:
        key = "assembly-source-block:" + document_key(batch.supplier, batch.number)
    except BrowserError:
        return ""
    entry = db.get(AppSetting, key)
    return entry.value if entry else ""


def record_state(db, run, supplier, label, posted, deletion_mark, evidence):
    if type(posted) is not bool or type(deletion_mark) is not bool or len(evidence.strip()) < 5:
        raise BrowserError("Проверьте оба признака: проведение и пометку удаления; неизвестный статус не разрешает сохранение")
    key = document_key(supplier, label)
    observed = re.sub(r"нет\s+красн\w*\s+крест\w*|красн\w*\s+крест\w*\s+(?:нет|отсутствует)|без\s+красн\w*\s+крест\w*", "", evidence, flags=re.I)
    if re.search(r"красн\w*\s+крест|помечен\w*\s+на\s+удал|снять\s+пометку\s+удал", observed, re.I):
        deletion_mark = True
    data = scope(db, run)
    data.setdefault("document_states", {})[key] = {
        "posted": posted, "deletion_mark": deletion_mark, "evidence": evidence[:500]}
    if deletion_mark or not posted:
        reason = "Пометка удаления в 1С" if deletion_mark else "Документ не проведён в 1С"
        num = key.split("|")[1]
        data["skipped"][num] = {"kind": "deleted" if deletion_mark else "unposted", "reason": reason}
        block_source(db, supplier, label, reason)
    store(db, run, data)
    return "Документ исключён: пометка удаления или отсутствие проведения" if deletion_mark or not posted else "Статус проверен: проведён, пометки удаления нет; сохранение разрешено"


def require_eligible(db, run, supplier, label):
    from .models import AppSetting
    key = document_key(supplier, label)
    blocked = db.get(AppSetting, "assembly-source-block:" + key)
    if blocked:
        raise BrowserError("Сохранение запрещено: " + blocked.value)
    state = scope(db, run).get("document_states", {}).get(key)
    if not state or state.get("posted") is not True or state.get("deletion_mark") is not False:
        raise BrowserError("Перед сохранением проверьте значки строки и вызовите record_document_state: проведён=true, пометка удаления=false. Красный крестик запрещает сборку даже при зелёной отметке")


def seed_reported_deleted(db):
    from .models import AppSetting, ReturnBatch
    marker = "assembly-reported-deleted-7094-20261106"
    if db.get(AppSetting, marker):
        return
    from .assembly_returns import doc_date_of
    for batch in db.scalars(select(ReturnBatch)):
        try:
            key = document_key(batch.supplier, batch.number)
        except BrowserError:
            continue
        if "москвореч" in batch.supplier.casefold() and key.split("|")[1] == "7094" and str(doc_date_of(batch.number)) == "2026-11-06":
            block_source(db, batch.supplier, batch.number, "Пометка удаления в 1С, подтверждена владельцем на снимке журнала")
    db.add(AppSetting(key=marker, value="done"))
    db.commit()
