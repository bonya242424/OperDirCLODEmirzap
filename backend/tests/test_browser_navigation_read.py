import asyncio
from types import SimpleNamespace

import pytest

from app.browser import BrowserManager


def test_navigation_read_waits_and_retries_without_repeating_action():
    events = []
    async def evaluate(expression, *args):
        events.append("read")
        if events.count("read") < 3:
            raise RuntimeError("Page.evaluate: Execution context was destroyed, most likely because of a navigation")
        return {"ready": True}
    async def loaded(*args, **kwargs):
        events.append("loaded")
    async def pause(*args):
        events.append("pause")
    page = SimpleNamespace(evaluate=evaluate, wait_for_load_state=loaded, wait_for_timeout=pause)
    result = asyncio.run(BrowserManager()._evaluate_read(page, "() => document.title"))
    assert result == {"ready": True}
    assert events == ["read", "loaded", "pause", "read", "loaded", "pause", "read"]


@pytest.mark.parametrize("message, expected", [
    ("Execution context was destroyed", 3),
    ("Cannot find context with specified id", 3),
    ("Page closed", 1),
    ("unrelated script error", 1),
])
def test_navigation_read_is_bounded_and_does_not_hide_other_errors(message, expected):
    calls = []
    async def evaluate(*args):
        calls.append(1)
        raise RuntimeError(message)
    async def noop(*args, **kwargs):
        pass
    page = SimpleNamespace(evaluate=evaluate, wait_for_load_state=noop, wait_for_timeout=noop)
    with pytest.raises(RuntimeError, match=message):
        asyncio.run(BrowserManager()._evaluate_read(page, "() => document.title"))
    assert len(calls) == expected


def test_native_picker_matches_full_article_and_brand():
    from app.browser import exact_product_index
    rows = [
        {"article": "AR1130", "brand": "AREOL"},
        {"article": "AR113", "brand": "CHAMPION"},
        {"article": "AR113", "brand": "AREOL"},
    ]
    assert exact_product_index(rows, "AR113", "AREOL") == 2
    assert exact_product_index(rows, "AR11", "AREOL") is None
    assert exact_product_index(rows + [rows[2]], "AR113", "AREOL") is None
    assert exact_product_index([
        {"article": "6V0867174  9B9", "brand": "VAG"},
        {"article": "6V08671749B9", "brand": "FORD"},
    ], "6V08671749B9", "VAG") == 0


def test_native_search_resets_previous_selection_preserves_dates_and_selects_article():
    from app.browser import _Session
    events = []

    class Element:
        def __init__(self, kind="article"):
            self.kind = kind
        async def count(self):
            return 1
        async def evaluate(self, *args):
            return {"tag": "INPUT", "type": "text", "name": "MATNR", "label": ""}
        async def fill(self, value):
            events.append(("fill", self.kind, value))
        async def press(self, value):
            events.append(("press", self.kind, value))
        async def press_sequentially(self, value, **kwargs):
            events.append(("type", value))
        async def input_value(self):
            return "6V08671749B9"
        def nth(self, index):
            assert index == 0
            return self
        def locator(self, selector):
            assert selector == "span"
            return self
        @property
        def first(self):
            return self
        async def click(self):
            events.append(("select_article",))

    product = {"article": "6V0867174  9B9", "brand": "VAG"}
    async def evaluate(script, *args):
        if "dates:" in script:
            return {"selected": True, "dates": [
                {"id": "date-with", "value": "08.09.2026"},
                {"id": "date-to", "value": "08.11.2026"}]}
        if "query =>" in script:
            return ["6V0867174  9B9 VAG"]
        return [product]
    async def goto(url, **kwargs):
        events.append(("reset", url))
    async def noop(*args, **kwargs):
        return []
    page = SimpleNamespace(
        url="https://etp.armtek.ru/order_return", evaluate=evaluate,
        goto=goto, wait_for_timeout=noop,
        locator=lambda selector: Element(selector if "date-" in selector else "article"))
    manager = BrowserManager()
    manager._sessions[1] = _Session(None, page)
    manager._controls = noop
    result = asyncio.run(manager._search_article(
        1, "", "6V08671749B9", article="6V08671749B9", brand="VAG"))
    assert result["selected"] is True
    assert result["selected_article"] == "6V0867174  9B9"
    assert result["input_value"] == "6V08671749B9"
    assert events[0] == ("reset", "https://etp.armtek.ru/order_return")
    assert ("fill", '[id="date-with"]', "08.09.2026") in events
    assert ("fill", '[id="date-to"]', "08.11.2026") in events
    assert events.count(("select_article",)) == 1
