from sqlalchemy import select

from app.db import SessionLocal
from app.models import AppSetting, ReturnGuide, ReturnSupplier
from app.return_suppliers_seed import GUIDES_KEY, NAMES, SEED_KEY, sync


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


def test_guides_are_added_to_already_seeded_cards_and_visible_in_api(client, owner):
    with SessionLocal() as db:
        db.add(AppSetting(key=SEED_KEY, value="done"))  # предыдущее развёртывание создало только карточки
        suppliers = [ReturnSupplier(name=name) for name in NAMES]
        db.add_all(suppliers)
        other = ReturnSupplier(name="Армтек", rules="Правила владельца")
        db.add(other)
        db.flush()
        db.add(ReturnGuide(supplier_id=other.id, filename="Армтек.webm", disk_name="video", size=10, status="ok", text="Моя инструкция"))
        db.commit()
        sync(db)
        assert db.get(AppSetting, GUIDES_KEY)
        assert db.query(ReturnSupplier).count() == 10
        ids = {s.name: s.id for s in suppliers}
    for name, sid in ids.items():
        response = client.get(f"/api/returns/suppliers/{sid}/guides", headers=owner)
        assert response.status_code == 200
        guide = response.json()[0]
        assert guide["status"] == "ok" and len(guide["text"]) > 1000
        assert guide["filename"] == f"Инструкция по видео: {name}"
        assert guide["has_video"] is False
        assert "Количество брать из текущей сборки" in guide["text"]
    with SessionLocal() as db:
        assert db.query(ReturnGuide).filter_by(text="Моя инструкция").count() == 1
        assert db.query(ReturnSupplier).filter_by(name="Армтек").one().rules == "Правила владельца"


def test_text_guides_are_editable_and_not_overwritten_or_recreated(client, owner):
    with SessionLocal() as db:
        sync(db)
        guide = db.query(ReturnGuide).first()
        gid, sid = guide.id, guide.supplier_id
    url = f"/api/returns/guides/{gid}"
    assert client.patch(url, headers=owner, json={"text": "Уточнённая инструкция владельца"}).status_code == 200
    assert client.post(url + "/reanalyze", headers=owner).status_code == 409
    with SessionLocal() as db:
        sync(db)
        assert db.get(ReturnGuide, gid).text == "Уточнённая инструкция владельца"
        assert db.query(ReturnGuide).count() == 9
    assert client.delete(url, headers=owner).status_code == 204
    with SessionLocal() as db:
        sync(db)
        assert db.query(ReturnGuide).count() == 8
    assert client.get(f"/api/returns/suppliers/{sid}/guides", headers=owner).json() == []
