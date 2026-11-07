"""Карточки и подготовленные по видео инструкции поставщиков."""

import json
import logging
from pathlib import Path

from sqlalchemy import select

from .assembly_returns import supplier_by_name
from .models import AppSetting, ReturnGuide, User

SEED_KEY = "return-suppliers-videos-20261107"
GUIDES_KEY = "return-supplier-guides-20261107-v1"
log = logging.getLogger(__name__)
NAMES = (
    "Росско", "Автоконтинент", "Берг", "Шате",
    "москворечье возвраты (1)", "Партком",
    "фаворит возвраты (1)", "мотех", "ИКСОРА",
)


def sync(db):
    owner_id = db.scalar(select(User.id).where(User.role == "owner").order_by(User.id).limit(1))
    if owner_id is None:
        return
    if not db.get(AppSetting, SEED_KEY):
        for name in NAMES:
            supplier_by_name(db, name, owner_id)
        db.add(AppSetting(key=SEED_KEY, value="done"))
    if not db.get(AppSetting, GUIDES_KEY):
        content = json.loads((Path(__file__).parent / "data" / "supplier_return_guides.json").read_text(encoding="utf-8"))
        for item in content:
            supplier = supplier_by_name(db, item["supplier"], owner_id)
            db.add(ReturnGuide(
                supplier_id=supplier.id, filename=f"Инструкция по видео: {item['supplier']}",
                disk_name="", size=0, status="ok", text=item["text"], created_by=owner_id,
                message="Подготовленная текстовая инструкция по видео. Можно редактировать; недостающие этапы отмечены в тексте.",
            ))
        db.add(AppSetting(key=GUIDES_KEY, value="done"))
        log.info("Добавлены подготовленные инструкции возвратов: %s", len(content))
    db.commit()
