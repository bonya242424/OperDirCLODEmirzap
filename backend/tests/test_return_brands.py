import json
from types import SimpleNamespace

import pytest

from app.browser import BrowserError, exact_product_index
from app.return_brands import BRAND_RULE, MARKER, effective_brand, remember_selection, selected_indices, sync_instructions
from app.return_search import matching_offers
from app.return_submission import validate_preview


@pytest.mark.parametrize("article,original,site", [
    ("50013081", "KOLBENSCHMIDT", "KS ФИЛЬТРА"),
    ("305664", "GKN", "LOBRO"),
    ("6V08671749B9", "VAG", "VW"),
])
def test_sole_brand_is_accepted_only_for_full_article(article, original, site):
    candidates = [{"article": article, "brand": site}, {"article": article + "2", "brand": original}]
    assert selected_indices(candidates, article, original) == [0]
    assert exact_product_index(candidates, article, original) == 0
    candidates.append({"article": article, "brand": "OTHER"})
    assert selected_indices(candidates, article, original) == []
    assert exact_product_index(candidates, article, original) is None
    candidates.append({"article": article, "brand": original})
    assert selected_indices(candidates, article, original) == [3]


def test_forum_single_variant_across_multiple_deliveries_and_ambiguous_variants():
    row = SimpleNamespace(article="50013081", brand="KOLBENSCHMIDT")
    offers = [
        {"product": "50013081 KS ФИЛЬТРА Фильтр", "quantity": 1, "document": "A"},
        {"product": "50013081 KS ФИЛЬТРА Фильтр", "quantity": 2, "document": "B"},
        {"product": "500130812 OTHER", "quantity": 10},
    ]
    assert matching_offers(offers, row) == offers[:2]
    assert matching_offers(offers + [{"product": "50013081 OTHER Фильтр", "quantity": 0}], row) == []
    assert matching_offers([{"product": "305664 LOBRO ШРУС", "quantity": 1}], SimpleNamespace(article="305664", brand="GKN"))


def test_alias_evidence_is_scoped_and_final_form_still_checks_quantity():
    row = SimpleNamespace(article="50013081", brand="KOLBENSCHMIDT", qty=1, invoice_no="A", check_evidence="")
    preview = {"url": "https://itrade.forum-auto.ru/shop/returns/new", "label": "Отправить заявку",
        "text": "Создание нового возврата 50013081 KS ФИЛЬТРА Фильтр", "values": "Отказ клиента", "quantities": [1]}
    with pytest.raises(BrowserError):
        validate_preview(preview, [row], "itrade.forum-auto.ru")
    remember_selection(row, "KS ФИЛЬТРА Фильтр")
    validate_preview(preview, [row], "itrade.forum-auto.ru")
    preview["quantities"] = [2]
    with pytest.raises(BrowserError):
        validate_preview(preview, [row], "itrade.forum-auto.ru")
    row.invoice_no = "B"
    assert effective_brand(row) == "KOLBENSCHMIDT"
    preview["quantities"] = [1]
    with pytest.raises(BrowserError):
        validate_preview(preview, [row], "itrade.forum-auto.ru")


def test_armtek_final_composition_accepts_confirmed_site_brand():
    row = SimpleNamespace(article="305664", brand="GKN", qty=1, invoice_no="ЮП123456 /3", check_evidence="")
    remember_selection(row, "LOBRO")
    preview = {"url": "https://etp.armtek.ru/invoice/viewReturnInfo/1/2", "label": "Оформить возврат",
        "text": "Оформить возврат по фактуре", "values": "Отказ клиента", "page_text": "УПД ЮП123456 /3",
        "items": [{"article": "305664", "brand": "LOBRO", "qty": 1}]}
    validate_preview(preview, [row], "etp.armtek.ru")
    preview["items"][0]["brand"] = "OTHER"
    with pytest.raises(BrowserError):
        validate_preview(preview, [row], "etp.armtek.ru")


def test_forum_alias_passes_search_then_submission(client, owner, monkeypatch):
    from app.db import SessionLocal
    from app.models import ReturnLine
    from tests.test_return_submission import setup_submission
    bid, lid, browser, script = setup_submission(client, owner, monkeypatch)
    with SessionLocal() as db:
        row = db.get(ReturnLine, lid)
        row.article, row.brand, row.qty = "50013081", "KOLBENSCHMIDT", 1
        db.commit()
    browser.preview.update(text="Создание нового возврата 50013081 KS ФИЛЬТРА Фильтр", quantities=[1])
    browser.forum_returns = lambda *a: {"has_creation_section": True, "search_values": ["50013081"], "offers": [
        {"product": "50013081 KS ФИЛЬТРА Фильтр", "quantity": 1, "href": "https://itrade.forum-auto.ru/shop/returns/new", "control": "1-2"}
    ]}
    from tests.test_returns_run import call
    script.steps.insert(0, call("forum_available_returns", line_id=lid))
    response = client.post(f"/api/returns/batches/{bid}/submit-run", headers=owner, json={"confirmed": True})
    assert response.status_code == 202
    assert browser.clicks == 1
    with SessionLocal() as db:
        row = db.get(ReturnLine, lid)
        assert row.status == "submitted" and row.request_no == "987654321"
        assert row.brand == "KOLBENSCHMIDT"
        assert "KS ФИЛЬТРА" in effective_brand(row)


def test_instructions_are_saved_for_every_supplier_without_erasing_steps(client, owner):
    from app.db import SessionLocal
    from app.models import ReturnSupplier, ReturnGuide
    from tests.test_returns_run import _batch
    _batch(client, owner, "https://itrade.forum-auto.ru")
    with SessionLocal() as db:
        supplier = db.query(ReturnSupplier).first()
        guide = ReturnGuide(supplier_id=supplier.id, filename="Шаги", disk_name="", size=0, status="ok", text="Открой возвраты", created_by=supplier.created_by)
        db.add(guide)
        db.flush()
        sync_instructions(db)
        sync_instructions(db)
        db.commit()
        assert supplier.rules.count(MARKER) == 1
        assert guide.text.count(MARKER) == 1
        assert "Открой возвраты" in guide.text

def test_unknown_brand_does_not_make_another_variant_unique():
    assert selected_indices([
        {"article": "305664", "brand": "LOBRO"},
        {"article": "305664", "brand": ""},
    ], "305664", "GKN") == []
