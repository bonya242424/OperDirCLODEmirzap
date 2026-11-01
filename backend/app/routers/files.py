import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..deps import current_user
from ..integrations import service
from ..integrations.registry import BY_KEY
from ..models import Folder, StoredFile, User, utcnow
from ..schemas import _utc

router = APIRouter(prefix="/api", tags=["files"])
CHUNK = 1024 * 1024


class FolderIn(BaseModel):
    name: str
    description: str = ""
    source_key: str | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) > 120:
            raise ValueError("Название папки: от 1 до 120 символов")
        return v


class FolderPatch(BaseModel):
    name: str | None = None
    description: str | None = None


class FolderOut(BaseModel):
    id: int
    name: str
    description: str
    source_key: str | None
    files_count: int
    last_upload_at: datetime | None
    can_edit: bool

    _fix = field_validator("last_upload_at")(_utc)


class FileOut(BaseModel):
    id: int
    folder_id: int
    name: str
    size: int
    content_type: str
    uploaded_by: str
    uploaded_at: datetime
    can_delete: bool

    _fix = field_validator("uploaded_at")(_utc)


def _root() -> Path:
    root = Path(settings.files_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_name(raw: str | None) -> str:
    name = os.path.basename((raw or "").replace("\\", "/")).strip()
    name = re.sub(r"[\x00-\x1f]", "", name)
    return name[:200] or "file"


def _can_edit_folder(folder: Folder, me: User) -> bool:
    return me.role == "owner" or folder.created_by == me.id


def _folder(db: Session, folder_id: int) -> Folder:
    folder = db.get(Folder, folder_id)
    if folder is None:
        raise HTTPException(status_code=404, detail="Папка не найдена")
    return folder


def _folder_out(db: Session, f: Folder, me: User) -> FolderOut:
    count, last = db.execute(
        select(func.count(StoredFile.id), func.max(StoredFile.uploaded_at)).where(StoredFile.folder_id == f.id)
    ).one()
    return FolderOut(
        id=f.id,
        name=f.name,
        description=f.description,
        source_key=f.source_key,
        files_count=count,
        last_upload_at=last,
        can_edit=_can_edit_folder(f, me),
    )


def _file_out(row: StoredFile, me: User, names: dict[int, str]) -> FileOut:
    return FileOut(
        id=row.id,
        folder_id=row.folder_id,
        name=row.name,
        size=row.size,
        content_type=row.content_type,
        uploaded_by=names.get(row.uploaded_by, ""),
        uploaded_at=row.uploaded_at,
        can_delete=me.role == "owner" or row.uploaded_by == me.id,
    )


@router.get("/folders", response_model=list[FolderOut])
def list_folders(db: Session = Depends(get_db), me: User = Depends(current_user)):
    return [_folder_out(db, f, me) for f in db.scalars(select(Folder).order_by(Folder.name))]


@router.post("/folders", response_model=FolderOut, status_code=201)
def create_folder(body: FolderIn, db: Session = Depends(get_db), me: User = Depends(current_user)):
    if body.source_key and body.source_key not in BY_KEY:
        raise HTTPException(status_code=422, detail="Неизвестный источник данных")
    if db.scalar(select(Folder).where(Folder.name == body.name)):
        raise HTTPException(status_code=409, detail="Папка с таким названием уже есть")
    folder = Folder(name=body.name, description=body.description.strip(), source_key=body.source_key, created_by=me.id)
    db.add(folder)
    db.commit()
    return _folder_out(db, folder, me)


@router.patch("/folders/{folder_id}", response_model=FolderOut)
def update_folder(folder_id: int, body: FolderPatch, db: Session = Depends(get_db), me: User = Depends(current_user)):
    folder = _folder(db, folder_id)
    if not _can_edit_folder(folder, me):
        raise HTTPException(status_code=403, detail="Менять папку может её создатель или владелец системы")
    if body.name is not None:
        name = body.name.strip()
        if not name or len(name) > 120:
            raise HTTPException(status_code=422, detail="Название папки: от 1 до 120 символов")
        clash = db.scalar(select(Folder).where(Folder.name == name, Folder.id != folder.id))
        if clash:
            raise HTTPException(status_code=409, detail="Папка с таким названием уже есть")
        folder.name = name
    if body.description is not None:
        folder.description = body.description.strip()
    db.commit()
    return _folder_out(db, folder, me)


@router.delete("/folders/{folder_id}", status_code=204)
def delete_folder(folder_id: int, db: Session = Depends(get_db), me: User = Depends(current_user)):
    folder = _folder(db, folder_id)
    if not _can_edit_folder(folder, me):
        raise HTTPException(status_code=403, detail="Удалить папку может её создатель или владелец системы")
    if db.scalar(select(func.count(StoredFile.id)).where(StoredFile.folder_id == folder.id)):
        raise HTTPException(status_code=409, detail="Сначала удалите файлы из папки")
    db.delete(folder)
    db.commit()


@router.get("/folders/{folder_id}/files", response_model=list[FileOut])
def list_files(folder_id: int, db: Session = Depends(get_db), me: User = Depends(current_user)):
    _folder(db, folder_id)
    names = {u.id: (u.full_name or u.email) for u in db.scalars(select(User))}
    rows = db.scalars(
        select(StoredFile).where(StoredFile.folder_id == folder_id).order_by(StoredFile.uploaded_at.desc(), StoredFile.id.desc())
    )
    return [_file_out(r, me, names) for r in rows]


@router.post("/folders/{folder_id}/files", response_model=FileOut, status_code=201)
async def upload(
    folder_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    me: User = Depends(current_user),
):
    folder = _folder(db, folder_id)
    limit = settings.max_upload_mb * 1024 * 1024
    disk_name = uuid.uuid4().hex
    target = _root() / disk_name
    size = 0
    try:
        with open(target, "wb") as out:
            while chunk := await file.read(CHUNK):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(status_code=413, detail=f"Файл больше {settings.max_upload_mb} МБ")
                out.write(chunk)
        if size == 0:
            raise HTTPException(status_code=422, detail="Файл пустой")
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    row = StoredFile(
        folder_id=folder.id,
        name=_safe_name(file.filename),
        size=size,
        content_type=file.content_type or "application/octet-stream",
        disk_name=disk_name,
        uploaded_by=me.id,
    )
    db.add(row)
    db.commit()
    if folder.source_key:
        service.record_sync(db, folder.source_key, row.uploaded_at or utcnow())
    names = {me.id: me.full_name or me.email}
    return _file_out(row, me, names)


def _stored(db: Session, file_id: int) -> StoredFile:
    row = db.get(StoredFile, file_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Файл не найден")
    return row


@router.get("/files/{file_id}/download")
def download(file_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    row = _stored(db, file_id)
    path = _root() / row.disk_name
    if not path.is_file():
        raise HTTPException(status_code=410, detail="Файл потерян на сервере")
    return FileResponse(
        path,
        media_type=row.content_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(row.name)}"},
    )


@router.delete("/files/{file_id}", status_code=204)
def delete_file(file_id: int, db: Session = Depends(get_db), me: User = Depends(current_user)):
    row = _stored(db, file_id)
    if not (me.role == "owner" or row.uploaded_by == me.id):
        raise HTTPException(status_code=403, detail="Удалить файл может тот, кто его загрузил, или владелец")
    (_root() / row.disk_name).unlink(missing_ok=True)
    db.delete(row)
    db.commit()
