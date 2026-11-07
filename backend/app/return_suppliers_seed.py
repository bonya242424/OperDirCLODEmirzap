"""Однократное создание карточек по названиям видео владельца."""

from sqlalchemy import select

from .assembly_returns import supplier_by_name
from .models import AppSetting, User

SEED_KEY = "return-suppliers-videos-20261107"
NAMES = (
    "Росско", "Автоконтинент", "Берг", "Шате",
    "москворечье возвраты (1)", "Партком",
    "фаворит возвраты (1)", "мотех", "ИКСОРА",
)


def sync(db):
    if db.get(AppSetting, SEED_KEY):
        return
    owner_id = db.scalar(select(User.id).where(User.role == "owner").order_by(User.id).limit(1))
    if owner_id is None:
        return
    for name in NAMES:
        supplier_by_name(db, name, owner_id)
    db.add(AppSetting(key=SEED_KEY, value="done"))
    db.commit()
