import json

import pytest

from app.db import SessionLocal
from app.models import ReturnLine
from app.return_search import article_variants, matching_offers
from tests.test_agents import Script
from tests.test_returns_run import _batch, _last, call, no_browser


def test_vag_spacing_and_article_only_variants():
    variants = article_variants("6V08671749B9", "VAG")
    assert "6V0867174 9B9" in variants
    assert "6V0 867 174 9B9" in variants
    assert "6V0867174" in variants
    assert article_variants("AR113", "AREOL")[0] == "AR113"
    assert all("AREOL" not in value for value in article_variants("AR113", "AREOL"))


def test_armtek_search_logs_actual_input_and_needs_fallback_before_not_found(client, owner, monkeypatch, no_browser):
    from app import returns_run
    bid = _batch(client, owner, "https://etp.armtek.ru")
    with SessionLocal() as db:
        row = db.query(ReturnLine).filter_by(batch_id=bid).order_by(ReturnLine.id).first()
        row.article, row.brand = "AR113", "AREOL"
        lid = row.id
        db.commit()
    inputs = []

    def search(key, control, value):
        inputs.append(value)
        return {"input_value": value, "suggestions": ["AR113 AREOL масло"], "controls": []}

    returns_run.manager.search_article = search
    script = Script(
        call("record_item_result", line_id=lid, status="not_found", note="Нет товара"),
        call("armtek_search_article", line_id=lid, control="1-1", variant=0),
        call("finish", reason="blocked", summary="Тест"),
    )
    monkeypatch.setattr("app.returns_run.get_provider", lambda: script)
    client.post(f"/api/returns/batches/{bid}/check-run", headers=owner)
    assert inputs == ["AR113"]
    logs = "\n".join(_last(client, owner, bid)["log"])
    assert "Не все варианты" in logs and "в поле артикула «AR113»" in logs
    assert "Подсказки Армтека: AR113 AREOL" in logs
    with SessionLocal() as db:
        assert db.get(ReturnLine, lid).status == "pending"
        assert json.loads(db.get(ReturnLine, lid).check_evidence)["searches"] == ["AR113"]


def test_forum_active_offer_cannot_be_marked_already_returned(client, owner, monkeypatch, no_browser):
    from app import returns_run
    bid = _batch(client, owner, "https://itrade.forum-auto.ru")
    with SessionLocal() as db:
        row = db.query(ReturnLine).filter_by(batch_id=bid).order_by(ReturnLine.id).first()
        row.article, row.brand, row.qty = "SB991", "TEKNOROT", 1
        lid = row.id
        db.commit()
    returns_run.manager.forum_returns = lambda *args: {"has_creation_section": True, "search_values": ["SB991"], "offers": [
        {"product": "SB 991 TEKNOROT Сайлентблок", "quantity": 1, "href": "https://itrade.forum-auto.ru/shop/returns/new", "control": "1-2", "document": "71575769 · 311026-1129"}
    ]}
    script = Script(
        call("forum_available_returns", line_id=lid),
        call("record_item_result", line_id=lid, status="already_returned", note="Старая заявка 71820526"),
        call("finish", reason="blocked", summary="Тест"),
    )
    monkeypatch.setattr("app.returns_run.get_provider", lambda: script)
    client.post(f"/api/returns/batches/{bid}/check-run", headers=owner)
    with SessionLocal() as db:
        assert db.get(ReturnLine, lid).status == "pending"
    assert "есть доступная поставка" in "\n".join(_last(client, owner, bid)["log"])
    result = script.seen[1][0][-1]["content"][0]["content"]
    assert json.loads(result)["offers"][0]["quantity"] == 1


def test_offers_match_full_article_and_brand():
    from types import SimpleNamespace
    row = SimpleNamespace(article="SB991", brand="TEKNOROT")
    offers = [{"product": "SB 991 TEKNOROT", "quantity": 1}, {"product": "SB991 OTHER", "quantity": 10}, {"product": "SB9912 TEKNOROT", "quantity": 5}]
    assert matching_offers(offers, row) == offers[:1]


def test_article_search_uses_keyboard_events_and_returns_actual_value():
    import asyncio
    from types import SimpleNamespace
    from app.browser import BrowserManager, _Session

    async def check():
        events = []

        async def info(*args):
            return {"tag": "INPUT", "type": "text", "label": "Код артикула (точный по номеру)"}

        async def fill(value):
            events.append(("clear", value))

        async def type_text(value, **kwargs):
            events.append(("keys", value))

        async def value():
            return "AR113"

        async def wait(*args):
            pass

        async def suggestions(*args):
            return ["AR113 AREOL"]

        async def element(*args):
            return SimpleNamespace(evaluate=info, fill=fill, press_sequentially=type_text, input_value=value)

        async def controls(*args):
            return [{"control": "2-1", "label": "AR113 AREOL"}]

        manager = BrowserManager()
        manager._sessions[1] = _Session(None, SimpleNamespace(wait_for_timeout=wait, evaluate=suggestions))
        manager._control, manager._controls = element, controls
        result = await manager._search_article(1, "1-1", "AR113")
        assert events == [("clear", ""), ("keys", "AR113")]
        assert result["input_value"] == "AR113" and result["suggestions"] == ["AR113 AREOL"]

    asyncio.run(check())
