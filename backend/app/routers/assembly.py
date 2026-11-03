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
from ..models import AssemblyRun, Connection, User

router = APIRouter(prefix="/api/assembly", tags=["assembly"])


class RunIn(BaseModel):
    connection_id: int
    date_from: date
    date_to: date
    note: str = ""


def _owner(me: User) -> None:
    if me.role != "owner":
        raise HTTPException(status_code=403, detail="Сборку из 1С запускает только владелец")


def _out(r: AssemblyRun, full: bool = False) -> dict:
    d = {"id": r.id, "connection_id": r.connection_id, "status": r.status, "summary": r.summary, "date_from": r.date_from, "date_to": r.date_to, "created_at": r.created_at, "finished_at": r.finished_at, "saved": json.loads(r.saved or "[]")}
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
    return _out(run, True)


@router.get("/runs")
def runs(db: Session = Depends(get_db), me: User = Depends(current_user)):
    _owner(me)
    return [_out(r) for r in db.scalars(select(AssemblyRun).order_by(AssemblyRun.id.desc()).limit(20))]


@router.get("/runs/{run_id}")
def one(run_id: int, db: Session = Depends(get_db), me: User = Depends(current_user)):
    _owner(me)
    r = db.get(AssemblyRun, run_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Запуск не найден")
    return _out(r, True)


@router.post("/runs/{run_id}/stop")
def stop(run_id: int, db: Session = Depends(get_db), me: User = Depends(current_user)):
    _owner(me)
    r = db.get(AssemblyRun, run_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Запуск не найден")
    if r.status == "running":
        r.status = "stopped"
        db.commit()
    return _out(r, True)
