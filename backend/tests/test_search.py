from app.agents.tools import Ctx, TOOLS
from app.db import SessionLocal
from app.llm import Step, ToolCall
from app.models import Agent
from app.search import stems
from tests.conftest import login


def _upload(client, h, name, data: bytes):
    fid = client.post("/api/folders", headers=h, json={"name": f"Папка {name}"}).json()["id"]
    r = client.post(f"/api/folders/{fid}/files", headers=h, files={"file": (name, data, "text/csv")})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_stems_handle_russian_forms():
    assert stems("возвратов поставщику") == ["возвра", "постав"]
    assert stems("и на по") == []


def test_search_files_tasks_returns(client, owner):
    _upload(client, owner, "заказы.csv", "Дата;Заказов\n02.11;120\nПоставщик Армтек;возврат деталей\n".encode())
    client.post("/api/board/tasks", headers=owner, json={"title": "Проверить возврат у Армтека", "description": "накладная 15"})
    with open("tests/fixtures/returns_sample.csv", "rb") as f:
        client.post("/api/returns/import", headers=owner, files={"file": ("r.csv", f.read(), "text/csv")})
    hits = client.get("/api/search", headers=owner, params={"q": "возвратов армтек"}).json()
    kinds = {h["type"] for h in hits}
    assert {"file", "task"} <= kinds
    assert any("заказы.csv" in h["title"] for h in hits)
    assert any(h["type"] == "task" and "Армтек" in h["title"] for h in hits)
    ret = client.get("/api/search", headers=owner, params={"q": "SLKH073"}).json()
    assert ret and ret[0]["type"] == "return" and "Алтуфьево" in ret[0]["snippet"]
    assert client.get("/api/search", headers=owner, params={"q": "абвгдеж"}).json() == []
    assert client.get("/api/search", headers=owner, params={"q": "а"}).status_code == 422
    assert client.get("/api/search", params={"q": "возврат"}).status_code in (401, 403)


def test_deleted_file_leaves_search(client, owner):
    fid = _upload(client, owner, "секрет.txt", "уникальноеслово123 в файле".encode())
    assert client.get("/api/search", headers=owner, params={"q": "уникальноеслово123"}).json()
    client.delete(f"/api/files/{fid}", headers=owner)
    assert client.get("/api/search", headers=owner, params={"q": "уникальноеслово123"}).json() == []


def test_xlsx_is_indexed(client, owner, tmp_path):
    from openpyxl import Workbook

    wb = Workbook()
    wb.active.append(["Склад", "Остаток"])
    wb.active.append(["Мытищи", 77])
    p = tmp_path / "o.xlsx"
    wb.save(p)
    _upload(client, owner, "остатки.xlsx", p.read_bytes())
    hits = client.get("/api/search", headers=owner, params={"q": "мытищи"}).json()
    assert hits and hits[0]["type"] == "file" and "Мытищи" in hits[0]["snippet"]


def test_search_tool_for_agents_and_chat(client, owner, monkeypatch):
    _upload(client, owner, "заказы.csv", "заказов за день 120".encode())
    seen = {}

    class Script:
        def __init__(self):
            self.n = 0

        def step(self, system, messages, tools):
            self.n += 1
            seen.setdefault("tools", [t["name"] for t in tools])
            if self.n == 1:
                c = ToolCall("t1", "search_company", {"query": "заказов"})
                return Step(calls=[c], assistant_content=[{"type": "tool_use", "id": "t1", "name": "search_company", "input": c.input}])
            seen["result"] = str(messages[-1]["content"])
            return Step(text="Нашёл: 120", assistant_content=[{"type": "text", "text": "Нашёл: 120"}])

    monkeypatch.setattr("app.routers.chat.get_provider", lambda: Script())
    r = client.post("/api/chat", headers=owner, json={"message": "сколько заказов?"})
    assert r.json()["reply"]["content"] == "Нашёл: 120"
    assert "search_company" in seen["tools"] and "заказы.csv" in seen["result"]
    assert "set_metric_value" not in seen["tools"]  # чат ничего не пишет
