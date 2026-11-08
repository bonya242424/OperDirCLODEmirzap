import asyncio
from types import SimpleNamespace

from app.return_lookup import LookupTrace, matches_request, response_shape
from tests.test_returns_run import no_browser


def request(url, body=None, kind="xhr"):
    return SimpleNamespace(url=url, post_data=body, resource_type=kind)


def test_lookup_matches_exact_article_and_origin_not_prefix_or_static_files():
    origin = "https://etp.armtek.ru/order_return"
    assert matches_request(request("https://etp.armtek.ru/search?q=AR113"), origin, "AR113")
    assert matches_request(request("https://etp.armtek.ru/search", '{"q":"6V0867174 9B9"}'), origin, "6V0867174 9B9")
    assert not matches_request(request("https://etp.armtek.ru/search?q=AR113"), origin, "AR11")
    assert not matches_request(request("https://other.example/search?q=AR113"), origin, "AR113")
    assert not matches_request(request("https://etp.armtek.ru/AR113.js", kind="script"), origin, "AR113")


def test_response_diagnostics_never_include_response_values():
    data = {"token": "DO-NOT-LOG", "data": [{"customer": "PRIVATE", "article": "AR113"}]}
    shape = response_shape(data)
    assert shape["arrays"] == {"$.data": 1}
    assert "DO-NOT-LOG" not in str(shape) and "PRIVATE" not in str(shape)
    assert response_shape({"error": "bad parameters"})["has_error"]


def test_trace_is_detached_and_only_summarizes_matching_lookup():
    async def check():
        handlers = {}
        page = SimpleNamespace(url="https://etp.armtek.ru/order_return",
                               on=lambda event, cb: handlers.update({event: cb}),
                               remove_listener=lambda event, cb: handlers.pop(event))
        trace = LookupTrace(page, "AR113")
        trace.start()

        async def data():
            return {"token": "SECRET", "items": []}

        handlers["response"](SimpleNamespace(url="https://etp.armtek.ru/search?q=AR113&token=SECRET", status=200,
            headers={"content-type": "application/json"}, request=request("https://etp.armtek.ru/search?q=AR113&token=SECRET"), json=data))
        result = await trace.snapshot()
        trace.close()
        assert not handlers
        assert result[0]["path"] == "/search" and result[0]["shape"]["arrays"] == {"$.items": 0}
        assert "SECRET" not in str(result)

    asyncio.run(check())


def test_unread_suggestions_cannot_be_reported_as_missing_product(client, owner, monkeypatch, no_browser):
    from app import returns_run
    from app.db import SessionLocal
    from app.models import ReturnLine
    from tests.test_agents import Script
    from tests.test_returns_run import _batch, _last, call
    bid = _batch(client, owner, "https://etp.armtek.ru")
    with SessionLocal() as db:
        row = db.query(ReturnLine).filter_by(batch_id=bid).first()
        row.article, row.brand = "AR113", "AREOL"
        db.commit()
        lid = row.id
    returns_run.manager.search_article = lambda key, control, value, **kwargs: {"input_value": value, "suggestions": [],
        "lookup_responses": [{"status": 200, "shape": {"arrays": {"$.items": 0}, "has_error": False}}]}
    script = Script(call("armtek_search_article", line_id=lid, variant=0),
                    call("armtek_search_article", line_id=lid, variant=1),
                    call("record_item_result", line_id=lid, status="not_found", note="Подсказки не прочитаны"),
                    call("finish", reason="blocked", summary="Тест"))
    monkeypatch.setattr(returns_run, "get_provider", lambda: script)
    client.post(f"/api/returns/batches/{bid}/check-run", headers=owner)
    assert "Отсутствие товара не подтверждено" in "\n".join(_last(client, owner, bid)["log"])
    with SessionLocal() as db:
        assert db.get(ReturnLine, lid).status == "pending"
