"""Раздел «Файлы → Контекст проекта»: документы о системе, чтобы ассистент мог их найти и прочитать.

Тексты лежат в app/project_context и обновляются в базе при старте сервера, если изменились.
В них нет паролей, ключей и токенов.
"""

import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import search as search_index
from .config import settings
from .models import Folder, StoredFile, User

FOLDER = "Контекст проекта"
SRC = Path(__file__).parent / "project_context"


def sync(db: Session) -> None:
    owner = db.scalar(select(User).where(User.role == "owner").order_by(User.id))
    docs = sorted(SRC.glob("*.txt")) if SRC.is_dir() else []
    if owner is None or not docs:
        return
    folder = db.scalar(select(Folder).where(Folder.name == FOLDER))
    if folder is None:
        folder = Folder(name=FOLDER, description="О системе OperDir: правила, что сделано, устройство, что осталось. Обновляется при выкладке", created_by=owner.id)
        db.add(folder)
        db.flush()
    root = Path(settings.files_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    have = {f.name: f for f in db.scalars(select(StoredFile).where(StoredFile.folder_id == folder.id))}
    for path in docs:
        data = path.read_bytes()
        row = have.get(path.name)
        if row is not None:
            target = root / row.disk_name
            if target.is_file() and target.read_bytes() == data:
                continue
            target.write_bytes(data)
            row.size = len(data)
        else:
            disk = uuid.uuid4().hex
            (root / disk).write_bytes(data)
            row = StoredFile(folder_id=folder.id, name=path.name, size=len(data), content_type="text/plain; charset=utf-8", disk_name=disk, uploaded_by=owner.id)
            db.add(row)
        db.commit()
        search_index.index_file(db, row)
