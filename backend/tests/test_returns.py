from pathlib import Path

SAMPLE = Path(__file__).parent / "fixtures" / "returns_sample.csv"


def _upload(client, owner, data: bytes, name="r.csv"):
    return client.post("/api/returns/import", headers=owner, files={"file": (name, data, "text/csv")})


def test_import_sample_summary(client, owner):
    r = _upload(client, owner, SAMPLE.read_bytes())
    assert r.status_code == 201, r.text
    s = r.json()
    assert s["supplier"] == "Армтек" and s["number"].startswith("№000007007")
    assert s["positions"] == 38 and s["qty"] == 48
    assert len(s["invoices"]) == 10 and len(s["warehouses"]) == 12
    assert s["reasons"] == [{"reason": "Отказ клиента", "qty": 48}]
    assert s["problems"] == []
    inv = {i["invoice_no"]: i for i in s["invoices"]}
    assert inv["ЮП0655627 /3"]["positions"] == 7 and inv["ЮП0655627 /3"]["qty"] == 9
    assert s["warehouses"][0]["warehouse"] == "Мытищи"
    assert len(client.get("/api/returns/batches", headers=owner).json()) == 1
    lines = client.get(f"/api/returns/batches/{s['id']}/lines", headers=owner).json()
    assert lines[0]["article"] == "SLKH073" and lines[0]["status"] == "pending"


def test_missing_reason_is_reported(client, owner):
    csv_text = "Сборка,№1\nПоставщик,Тест\n\nТовар,Кол. по документу,Номер счета,причина возврата\nA1,2,X1,\n"
    s = _upload(client, owner, csv_text.encode()).json()
    assert s["problems"] == ["У 1 строк нет причины возврата"]


def test_bad_files_rejected(client, owner):
    assert _upload(client, owner, b"abc,def\n1,2\n").status_code == 422
    bad = "Поставщик,Т\nТовар,Кол. по документу,Номер счета\nA1,много,X\n"
    assert "не число" in _upload(client, owner, bad.encode()).json()["detail"]
    assert _upload(client, owner, "Поставщик,Т\nТовар,Кол. по документу,Номер счета\n".encode()).status_code == 422
    assert client.post("/api/returns/import-url", headers=owner, json={"url": "https://evil.example/x"}).status_code == 422


def test_requires_login_and_delete(client, owner):
    assert client.get("/api/returns/batches").status_code in (401, 403)
    s = _upload(client, owner, SAMPLE.read_bytes()).json()
    assert client.delete(f"/api/returns/batches/{s['id']}", headers=owner).status_code == 204
    assert client.get("/api/returns/batches", headers=owner).json() == []
