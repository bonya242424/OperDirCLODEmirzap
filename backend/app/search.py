"""Общий поиск по данным компании: файлы, задачи, показатели, возвраты, итоги агентов.

Поиск по основам слов: «возвратов» находит «возврат» и «возвраты». Ранжирование простое:
чем больше слов запроса встретилось и чем плотнее они рядом, тем выше. Без внешних сервисов.
"""

import csv
import io
import re
from pathlib import Path

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from .config import settings
from .models import AgentRun, BitrixTask, Metric, ReturnBatch, ReturnLine, SearchChunk, StoredFile, Task

CHUNK = 1500
MAX_FILE_CHARS = 400_000
MAX_XLSX_ROWS = 5000
STOP = {"и", "в", "на", "по", "с", "к", "о", "не", "что", "как", "за", "из", "для", "это", "а", "но", "или", "у", "от", "до"}
TEXT_SUFFIXES = {".csv", ".txt", ".tsv", ".json", ".md", ".log"}


def stems(query: str) -> list[str]:
    out: list[str] = []
    for w in re.findall(r"[\w]+", query.lower()):
        if w in STOP or len(w) < 2:
            continue
        s = w[:6] if len(w) > 6 else (w[:-1] if len(w) > 4 else w)
        if s not in out:
            out.append(s)
    return out[:8]


def extract_text(path: Path, name: str) -> str:
    suffix = Path(name).suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook

        fh = open(path, "rb")  # у файла на диске нет расширения: openpyxl по имени формат не определит
        wb = load_workbook(fh, read_only=True, data_only=True)
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        try:
            for ws in wb.worksheets[:5]:
                w.writerow([f"[лист {ws.title}]"])
                for i, row in enumerate(ws.iter_rows(values_only=True)):
                    if i >= MAX_XLSX_ROWS:
                        break
                    w.writerow(["" if c is None else c for c in row])
        finally:
            wb.close()
            fh.close()
        return buf.getvalue()[:MAX_FILE_CHARS]
    if suffix in TEXT_SUFFIXES:
        return path.read_bytes()[: MAX_FILE_CHARS * 2].decode("utf-8-sig", errors="replace")[:MAX_FILE_CHARS]
    return ""


def index_file(db: Session, f: StoredFile) -> int:
    """Переиндексирует файл. Возвращает число кусков. Нечитаемый файл просто остаётся без индекса."""
    db.execute(delete(SearchChunk).where(SearchChunk.file_id == f.id))
    path = Path(settings.files_dir).resolve() / f.disk_name
    try:
        text = extract_text(path, f.name) if path.is_file() else ""
    except Exception:  # noqa: BLE001
        text = ""
    n = 0
    for i in range(0, len(text), CHUNK):
        part = text[i : i + CHUNK]
        if part.strip():
            db.add(SearchChunk(file_id=f.id, ord=n, text=part, norm=part.lower()))
            n += 1
    db.commit()
    return n


def drop_file(db: Session, file_id: int) -> None:
    db.execute(delete(SearchChunk).where(SearchChunk.file_id == file_id))


def index_missing(db: Session) -> int:
    done = set(db.scalars(select(SearchChunk.file_id).distinct()))
    total = 0
    for f in db.scalars(select(StoredFile)):
        if f.id not in done:
            total += index_file(db, f)
    return total


def _snippet(text: str, terms: list[str], width: int = 160) -> str:
    low = text.lower()
    pos = min((p for t in terms if (p := low.find(t)) >= 0), default=0)
    start = max(0, pos - width // 3)
    s = " ".join(text[start : start + width].split())
    return ("…" if start else "") + s + ("…" if start + width < len(text) else "")


def _score(text: str, terms: list[str]) -> int:
    low = text.lower()
    hit = sum(1 for t in terms if t in low)
    return hit * 10 + min(low.count(terms[0]) if terms else 0, 5)


def _like(col, terms):
    return or_(*[col.like(f"%{t}%") for t in terms])


def search(db: Session, query: str, limit: int = 10) -> list[dict]:
    terms = stems(query)
    if not terms:
        return []
    hits: list[tuple[int, dict]] = []

    def add(kind: str, ident: str, title: str, text: str) -> None:
        s = _score(f"{title} {text}", terms)
        if s:
            hits.append((s, {"type": kind, "id": ident, "title": title[:200], "snippet": _snippet(text or title, terms)}))

    for c, f in db.execute(
        select(SearchChunk, StoredFile).join(StoredFile, StoredFile.id == SearchChunk.file_id).where(_like(SearchChunk.norm, terms)).limit(300)
    ):
        add("file", str(f.id), f"Файл: {f.name}", c.text)
    for t in db.scalars(select(Task).where(_like(Task.title, terms) | _like(Task.description, terms)).limit(100)):
        add("task", f"l-{t.id}", f"Задача: {t.title}", t.description)
    for b in db.scalars(select(BitrixTask).where(_like(BitrixTask.title, terms) | _like(BitrixTask.description, terms)).limit(100)):
        add("task", f"b-{b.id}", f"Задача Битрикс24: {b.title}", b.description)
    for m in db.scalars(select(Metric).where(Metric.is_active.is_(True), _like(Metric.name, terms) | _like(Metric.description, terms)).limit(50)):
        add("metric", str(m.id), f"Показатель: {m.name}", m.description)
    for r in db.execute(
        select(ReturnLine, ReturnBatch).join(ReturnBatch, ReturnBatch.id == ReturnLine.batch_id).where(
            _like(ReturnLine.article, terms) | _like(ReturnLine.name, terms) | _like(ReturnLine.brand, terms) | _like(ReturnLine.invoice_no, terms)
        ).limit(100)
    ):
        ln, batch = r
        add("return", str(ln.id), f"Возврат {batch.supplier}: {ln.article}", f"{ln.name} {ln.brand}, {ln.qty} шт., счёт {ln.invoice_no}, {ln.warehouse}, {ln.reason}, статус {ln.status}")
    for run in db.scalars(select(AgentRun).where(AgentRun.status == "done", _like(AgentRun.summary, terms)).order_by(AgentRun.id.desc()).limit(50)):
        add("agent_run", str(run.id), "Итог запуска агента", run.summary)
    hits.sort(key=lambda h: -h[0])
    return [h[1] for h in hits[: max(1, min(limit, 30))]]
