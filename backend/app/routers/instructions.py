"""Инструкции: разделы с описанием владельца и видео. По кадрам ИИ составляет шаги, владелец их правит."""

import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..deps import current_user
from ..guide_calc import analyze_instr_video, guides_dir
from ..models import InstrSection, InstrVideo, User

router = APIRouter(prefix="/api/instructions", tags=["instructions"])
VIDEO_EXT = {".mp4", ".mov", ".webm", ".m4v", ".mkv"}


def _owner(me: User) -> None:
    if me.role != "owner":
        raise HTTPException(status_code=403, detail="Инструкции ведёт владелец")


class SectionIn(BaseModel):
    title: str
    notes: str = ""
    area: str = "1c"

    @field_validator("title")
    @classmethod
    def _t(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) > 200:
            raise ValueError("Название раздела: от 1 до 200 символов")
        return v

    @field_validator("notes")
    @classmethod
    def _n(cls, v: str) -> str:
        return v.strip()[:20000]

    @field_validator("area")
    @classmethod
    def _a(cls, v: str) -> str:
        if v not in ("1c",):
            raise ValueError("Неизвестная область инструкций")
        return v


class SectionPatch(BaseModel):
    title: str | None = None
    notes: str | None = None


def _out(db: Session, s: InstrSection) -> dict:
    n = db.scalar(select(func.count(InstrVideo.id)).where(InstrVideo.section_id == s.id)) or 0
    return {"id": s.id, "area": s.area, "title": s.title, "notes": s.notes, "videos": n}


def _get(db: Session, section_id: int) -> InstrSection:
    s = db.get(InstrSection, section_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Раздел не найден")
    return s


@router.get("/sections")
def list_sections(area: str = "1c", db: Session = Depends(get_db), _: User = Depends(current_user)):
    return [_out(db, s) for s in db.scalars(select(InstrSection).where(InstrSection.area == area).order_by(InstrSection.title))]


@router.post("/sections", status_code=201)
def create_section(body: SectionIn, db: Session = Depends(get_db), me: User = Depends(current_user)):
    _owner(me)
    s = InstrSection(area=body.area, title=body.title, notes=body.notes, created_by=me.id)
    db.add(s)
    db.commit()
    return _out(db, s)


@router.patch("/sections/{section_id}")
def update_section(section_id: int, body: SectionPatch, db: Session = Depends(get_db), me: User = Depends(current_user)):
    _owner(me)
    s = _get(db, section_id)
    data = body.model_dump(exclude_unset=True)
    merged = SectionIn(title=data.get("title") or s.title, notes=data["notes"] if data.get("notes") is not None else s.notes, area=s.area)
    s.title, s.notes = merged.title, merged.notes
    db.commit()
    return _out(db, s)


@router.delete("/sections/{section_id}", status_code=204)
def delete_section(section_id: int, db: Session = Depends(get_db), me: User = Depends(current_user)):
    _owner(me)
    s = _get(db, section_id)
    for v in db.scalars(select(InstrVideo).where(InstrVideo.section_id == s.id)):
        (guides_dir() / v.disk_name).unlink(missing_ok=True)
        db.delete(v)
    db.delete(s)
    db.commit()


def _vout(v: InstrVideo) -> dict:
    return {
        "id": v.id, "filename": v.filename, "size": v.size, "status": v.status, "text": v.text,
        "message": v.message, "created_at": v.created_at.isoformat(),
    }


def _video(db: Session, video_id: int) -> InstrVideo:
    v = db.get(InstrVideo, video_id)
    if v is None:
        raise HTTPException(status_code=404, detail="Видео не найдено")
    return v


@router.get("/sections/{section_id}/videos")
def list_videos(section_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    _get(db, section_id)
    return [_vout(v) for v in db.scalars(select(InstrVideo).where(InstrVideo.section_id == section_id).order_by(InstrVideo.id.desc()))]


@router.post("/sections/{section_id}/videos", status_code=201)
async def upload_video(
    section_id: int, tasks: BackgroundTasks, file: UploadFile = File(...), db: Session = Depends(get_db), me: User = Depends(current_user)
):
    _owner(me)
    _get(db, section_id)
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
    v = InstrVideo(section_id=section_id, filename=name, disk_name=disk_name, size=size, created_by=me.id)
    db.add(v)
    db.commit()
    tasks.add_task(analyze_instr_video, v.id)
    db.refresh(v)
    return _vout(v)


class TextIn(BaseModel):
    text: str

    @field_validator("text")
    @classmethod
    def _t(cls, v: str) -> str:
        return v.strip()[:20000]


@router.patch("/videos/{video_id}")
def edit_video(video_id: int, body: TextIn, db: Session = Depends(get_db), me: User = Depends(current_user)):
    _owner(me)
    v = _video(db, video_id)
    v.text = body.text
    db.commit()
    return _vout(v)


@router.post("/videos/{video_id}/reanalyze", status_code=202)
def reanalyze(video_id: int, tasks: BackgroundTasks, db: Session = Depends(get_db), me: User = Depends(current_user)):
    _owner(me)
    v = _video(db, video_id)
    if v.status == "running":
        raise HTTPException(status_code=409, detail="Разбор уже идёт")
    v.status, v.message = "running", ""
    db.commit()
    tasks.add_task(analyze_instr_video, v.id)
    return _vout(v)


@router.delete("/videos/{video_id}", status_code=204)
def delete_video(video_id: int, db: Session = Depends(get_db), me: User = Depends(current_user)):
    _owner(me)
    v = _video(db, video_id)
    (guides_dir() / v.disk_name).unlink(missing_ok=True)
    db.delete(v)
    db.commit()
