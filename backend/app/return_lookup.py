"""Наблюдение запросов текущего артикула, без cookies, заголовков авторизации и значений ответа."""

import asyncio
import json
import re
from urllib.parse import parse_qs, unquote_plus, urlsplit


def contains_value(value, expected):
    if isinstance(value, str):
        return re.sub(r"[\s./-]+", "", value).casefold() == re.sub(r"[\s./-]+", "", expected).casefold()
    if isinstance(value, dict):
        return any(contains_value(v, expected) for v in value.values())
    if isinstance(value, list):
        return any(contains_value(v, expected) for v in value)
    return False


def matches_request(request, origin, query):
    url = urlsplit(request.url)
    if url.netloc != urlsplit(origin).netloc or request.resource_type not in ("xhr", "fetch"):
        return False
    values = parse_qs(url.query)
    body = request.post_data or ""
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        payload = parse_qs(body)
    return (contains_value(values, query) or contains_value(payload, query)
            or any(unquote_plus(part).casefold() == query.casefold() for part in url.path.split("/")))


def response_shape(data):
    """Только структура, количество элементов и признак ошибки. Никаких значений/токенов."""
    arrays = {}

    def walk(value, path, depth):
        if isinstance(value, list):
            arrays[path] = len(value)
        elif isinstance(value, dict) and depth < 3:
            for key, child in list(value.items())[:20]:
                walk(child, f"{path}.{key}", depth + 1)
    walk(data, "$", 0)
    error = isinstance(data, dict) and any(data.get(key) for key in ("error", "errors", "exception"))
    return {"kind": type(data).__name__, "arrays": arrays, "has_error": bool(error)}


class LookupTrace:
    def __init__(self, page, query):
        self.page, self.query, self.responses = page, query, []
        self.listener = self._response
        self.request_listener = self._request
        self.requests_seen = 0

    def _request(self, request):
        try:
            self.requests_seen += int(matches_request(request, self.page.url, self.query))
        except Exception:
            pass

    def _response(self, response):
        try:
            if matches_request(response.request, self.page.url, self.query):
                self.responses.append(response)
                self.responses = self.responses[-4:]
        except Exception:
            pass

    def start(self):
        if hasattr(self.page, "on"):
            self.page.on("response", self.listener)
            self.page.on("request", self.request_listener)

    def close(self):
        if hasattr(self.page, "remove_listener"):
            self.page.remove_listener("response", self.listener)
            self.page.remove_listener("request", self.request_listener)

    async def snapshot(self):
        result = []
        for response in self.responses:
            record = {"path": urlsplit(response.url).path[:160], "status": response.status}
            if "json" in response.headers.get("content-type", "").lower():
                try:
                    record["shape"] = response_shape(await asyncio.wait_for(response.json(), timeout=1.5))
                except Exception:
                    record["unreadable_json"] = True
            result.append(record)
        return result
