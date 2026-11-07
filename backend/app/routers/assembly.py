"""Запуск сборки из 1С агентом (только владелец)."""

import json
import threading
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..assembly_run import run_assembly
from ..db import get_db
from ..deps import current_user
from ..assembly_run import add_to_knowledge
from ..models import AssemblyQuestion, AssemblyRun, Connection, User, utcnow

router = APIRouter(prefix="/api/assembly", tags=["assembly"])


class RunIn(BaseModel):
    connection_id: int
    date_from: date
    date_to: date
    note: str = ""


def _owner(me: User) -> None:
    if me.role != "owner":
        raise HTTPException(status_code=403, detail="Сборку из 1С запускает только владелец")


def _qa(db: Session, run_id: int) -> list[dict]:
    rows = db.scalars(select(AssemblyQuestion).where(AssemblyQuestion.run_id == run_id).order_by(AssemblyQuestion.id)).all()
    return [{"id": q.id, "question": q.question, "answer": q.answer} for q in rows]


def _out(r: AssemblyRun, full: bool = False, db: Session | None = None) -> dict:
    d = {"id": r.id, "connection_id": r.connection_id, "status": r.status, "summary": r.summary, "date_from": r.date_from, "date_to": r.date_to, "created_at": r.created_at, "finished_at": r.finished_at, "saved": json.loads(r.saved or "[]")}
    d["note"] = r.note
    inventory = json.loads(r.inventory or "{}")
    d["inventory"] = {"total": len(inventory.get("numbers", [])), "complete": inventory.get("complete", False), "suppliers": inventory.get("suppliers", [])}
    if db is not None:
        d["qa"] = _qa(db, r.id)
        d["question"] = next((x for x in d["qa"] if x["answer"] is None), None)
    if full:
        d["log"] = json.loads(r.log or "[]")
    return d


@router.post("/run", status_code=202)
def start(body: RunIn, db: Session = Depends(get_db), me: User = Depends(current_user)):
    _owner(me)
    conn = db.get(Connection, body.connection_id)
    if conn is None or conn.kind != "rdp":
        raise HTTPException(status_code=422, detail="Выберите терминал из «Интеграции → Подключения»")
    if body.date_to < body.date_from or (body.date_to - body.date_from).days > 400:
        raise HTTPException(status_code=422, detail="Период указан неверно (не больше 400 дней)")
    if db.scalar(select(AssemblyRun.id).where(AssemblyRun.status == "running")):
        raise HTTPException(status_code=409, detail="Сборка уже идёт: дождитесь окончания или остановите её")
    run = AssemblyRun(connection_id=conn.id, date_from=body.date_from, date_to=body.date_to, note=body.note.strip()[:1000], created_by=me.id)
    db.add(run)
    db.commit()
    threading.Thread(target=run_assembly, args=(run.id,), daemon=True, name=f"assembly-{run.id}").start()
    return _out(run, True, db)


@router.get("/runs")
def runs(db: Session = Depends(get_db), me: User = Depends(current_user)):
    _owner(me)
    return [_out(r, False, db) for r in db.scalars(select(AssemblyRun).order_by(AssemblyRun.id.desc()).limit(30))]


@router.get("/runs/{run_id}")
def one(run_id: int, db: Session = Depends(get_db), me: User = Depends(current_user)):
    _owner(me)
    r = db.get(AssemblyRun, run_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Запуск не найден")
    return _out(r, True, db)


@router.post("/runs/{run_id}/stop")
def stop(run_id: int, db: Session = Depends(get_db), me: User = Depends(current_user)):
    _owner(me)
    r = db.get(AssemblyRun, run_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Запуск не найден")
    if r.status == "running":
        r.status = "stopped"
        db.commit()
    return _out(r, True, db)


@router.get("/saved")
def saved_files(db: Session = Depends(get_db), me: User = Depends(current_user)):
    """Всё, что агент сохранил в «Файлы» за все запуски: список не пропадает после перезапуска сервера."""
    from ..models import Folder, StoredFile

    _owner(me)
    out = []
    for fo in db.scalars(select(Folder).where(Folder.name.like("Сборка 1С — %")).order_by(Folder.name)):
        files = db.scalars(select(StoredFile).where(StoredFile.folder_id == fo.id).order_by(StoredFile.id.desc()).limit(300)).all()
        out.append({"supplier": fo.name.removeprefix("Сборка 1С — "), "folder_id": fo.id, "files": [{"id": f.id, "name": f.name} for f in files]})
    return out


class AnswerIn(BaseModel):
    answer: str


@router.post("/questions/{qid}/answer")
def answer(qid: int, body: AnswerIn, db: Session = Depends(get_db), me: User = Depends(current_user)):
    _owner(me)
    q = db.get(AssemblyQuestion, qid)
    if q is None:
        raise HTTPException(status_code=404, detail="Вопрос не найден")
    text = body.answer.strip()
    if not text:
        raise HTTPException(status_code=422, detail="Напишите ответ")
    if q.answer is not None:
        raise HTTPException(status_code=409, detail="На этот вопрос уже ответили")
    q.answer, q.answered_at = text[:3000], utcnow()
    add_to_knowledge(db, q)
    db.commit()
    return {"ok": True}
