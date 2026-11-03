from sqlalchemy import select

from app import project_context_seed as seed
from app.agents.tools import TOOLS, Ctx
from app.db import SessionLocal
from app.models import Folder, StoredFile


def test_context_files_seeded_and_readable(client, owner):
    with SessionLocal() as db:
        seed.sync(db)
        seed.sync(db)  # повтор не плодит дубли
        folder = db.scalar(select(Folder).where(Folder.name == seed.FOLDER))
        files = db.scalars(select(StoredFile).where(StoredFile.folder_id == folder.id)).all()
        assert len(files) == len(list(seed.SRC.glob("*.txt"))) >= 4
        text = TOOLS["read_file"].handler(Ctx(db, None, lambda _t: None), {"file_id": files[0].id})
        assert "OperDir" in text and "обрезано" not in text
        found = TOOLS["search_company"].handler(Ctx(db, None, lambda _t: None), {"query": "Сборка 1С"})
        assert "Контекст проекта" in found or "Что сделано" in found


def test_no_secrets_in_context_files():
    for p in seed.SRC.glob("*.txt"):
        t = p.read_text(encoding="utf-8").lower()
        assert "sk-" not in t and "oauth y0_" not in t
        assert "168.113" not in t and "185.73" not in t
