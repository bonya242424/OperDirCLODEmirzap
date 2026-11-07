from sqlalchemy import select

from app.db import SessionLocal
from app.models import AppSetting, ReturnSupplier
from app.return_suppliers_seed import NAMES, SEED_KEY, sync


def test_supplier_cards_seed_once_and_preserve_existing_settings(client, owner):
    with SessionLocal() as db:
        existing = ReturnSupplier(name="Росско", portal_url="https://example.com", rules="Мои правила")
        db.add(existing)
        db.commit()
        sync(db)
        sync(db)
        rows = list(db.scalars(select(ReturnSupplier)))
        assert {row.name for row in rows} == set(NAMES)
        assert len(rows) == len(NAMES)
        assert existing.portal_url == "https://example.com" and existing.rules == "Мои правила"
        assert all(not row.portal_url and not row.session_encrypted for row in rows if row.id != existing.id)
        assert db.get(AppSetting, SEED_KEY)
