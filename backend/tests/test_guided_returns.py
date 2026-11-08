from types import SimpleNamespace
import json

import pytest

from app.browser import BrowserError
from app.return_profiles import profile_for
from app.return_submission import supplier_kind, validate_preview
from app.returns_run import tools_for_supplier


@pytest.mark.parametrize("domain", ["lk.favorit-parts.ru", "berg.ru", "autokontinent.ru", "portal.moskvorechie.ru", "b2b.part-kom.ru", "motexc.ru", "b2b.ixora-auto.ru"])
def test_profiles_only_enable_documented_final_action(domain):
    assert supplier_kind("https://" + domain) == "guided"
    assert supplier_kind("https://" + domain + ".evil.test") is None


@pytest.mark.parametrize("domain", ["shate-m.ru", "msk.rossko.ru"])
def test_unverified_final_stage_is_not_treated_as_ready(domain):
    assert supplier_kind("https://" + domain) is None


def row():
    return SimpleNamespace(article="305664", brand="LOBRO", qty=1, invoice_no="123456-99", check_evidence="")


def preview():
    return {"url": "https://lk.favorit-parts.ru/return/new", "label": "Вернуть",
        "text": "Возврат товара, поставка 123456-99", "values": "Отказ",
        "root_scope": "form", "form_valid": True, "unmapped_quantities": [],
        "items": [{"article": "305664", "brand": "LOBRO", "qty": 1, "text": "305664 LOBRO"}]}


def test_guided_final_form_requires_exact_composition_and_source():
    data = preview()
    validate_preview(data, [row()], "lk.favorit-parts.ru")
    data["items"][0]["qty"] = 2
    with pytest.raises(BrowserError):
        validate_preview(data, [row()], "lk.favorit-parts.ru")
    data = preview()
    data["items"].append({"article": "OTHER", "brand": "LOBRO", "qty": 1})
    with pytest.raises(BrowserError):
        validate_preview(data, [row()], "lk.favorit-parts.ru")
    data = preview()
    data["text"] = "Возврат товара, поставка OTHER"
    with pytest.raises(BrowserError):
        validate_preview(data, [row()], "lk.favorit-parts.ru")


@pytest.mark.parametrize("changes", [{"root_scope": "body"}, {"form_valid": False}, {"unmapped_quantities": [2]}, {"label": "Сохранить"}])
def test_guided_unknown_quantity_or_draft_is_never_sent(changes):
    data = {**preview(), **changes}
    with pytest.raises(BrowserError):
        validate_preview(data, [row()], "lk.favorit-parts.ru")


def test_receipt_tools_are_supplier_scoped_and_never_offer_another_send():
    guided = {tool["name"] for tool in tools_for_supplier("guided", True, True)}
    assert {"record_submission", "record_confirmation"} <= guided
    assert "submit_return" not in guided
    assert "armtek_find_product" not in guided and "forum_available_returns" not in guided
    armtek = {tool["name"] for tool in tools_for_supplier("armtek", True, True)}
    assert "record_confirmation" not in armtek


def test_explicit_registration_without_number_is_locked_after_send(client, owner, monkeypatch):
    from tests.test_returns_run import _batch, call
    from tests.test_agents import Script
    from tests.test_return_submission import FormBrowser
    from app.db import SessionLocal
    from app.models import ReturnLine
    bid = _batch(client, owner, "https://lk.favorit-parts.ru")
    with SessionLocal() as db:
        line = db.query(ReturnLine).filter_by(batch_id=bid).first()
        lid = line.id
        data = preview()
        data["items"] = [{"article": line.article, "brand": line.brand, "qty": line.qty}]
        data["text"] = "Возврат товара, поставка " + line.invoice_no
    browser = FormBrowser(data)
    monkeypatch.setattr("app.returns_run.manager", browser)
    script = Script(call("submit_return", line_ids=[lid], x=10, y=10),
                    call("record_confirmation"),
                    call("finish", reason="blocked", summary="Остальные требуют проверки"))
    monkeypatch.setattr("app.returns_run.get_provider", lambda: script)
    result = client.post(f"/api/returns/batches/{bid}/submit-run", headers=owner, json={"confirmed": True})
    assert result.status_code == 202
    assert browser.clicks == 1
    with SessionLocal() as db:
        line = db.get(ReturnLine, lid)
        assert line.status == "submitted" and line.request_no == ""
        assert "без показанного номера" in line.note
        assert json.loads(line.check_evidence)["submission_confirmation"]
    client.post("/api/returns/check-queue/cancel", headers=owner)
    assert client.delete(f"/api/returns/batches/{bid}", headers=owner).status_code == 409
    client.post(f"/api/returns/batches/{bid}/reset-errors", headers=owner)
    with SessionLocal() as db:
        assert db.get(ReturnLine, lid).status == "submitted"


def test_source_document_prefix_is_not_a_match():
    data = preview()
    data["text"] = "Возврат товара, поставка 123456-990"
    with pytest.raises(BrowserError):
        validate_preview(data, [row()], "lk.favorit-parts.ru")


def test_generic_supplier_document_suffix_cannot_be_discarded():
    data = preview()
    data["text"] = "Возврат товара, поставка 123456/2"
    line = row()
    line.invoice_no = "123456/1"
    with pytest.raises(BrowserError):
        validate_preview(data, [line], "lk.favorit-parts.ru")


def test_only_same_guided_supplier_can_use_canonical_subdomain():
    from app.return_profiles import same_guided_site
    assert same_guided_site("https://www.favorit-parts.ru/returns", "https://lk.favorit-parts.ru")
    assert not same_guided_site("https://www.favorit-parts.ru.evil.test", "https://lk.favorit-parts.ru")
    assert not same_guided_site("https://berg.ru", "https://lk.favorit-parts.ru")
    assert not same_guided_site("http://lk.favorit-parts.ru", "https://lk.favorit-parts.ru")
    data = preview()
    data["url"] = "https://www.favorit-parts.ru/returns/new"
    validate_preview(data, [row()], "lk.favorit-parts.ru")
