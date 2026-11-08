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
