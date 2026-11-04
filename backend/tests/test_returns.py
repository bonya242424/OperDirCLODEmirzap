from app.db import SessionLocal
from app.models import ReturnSupplier
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


def _supplier(client, owner, **kw):
    body = {"name": "Армтек", "portal_url": "https://etp.armtek.ru", "contact": "Менеджер Иван", "rules": "Одна заявка на счёт", "default_reason": "Не подошёл"}
    body.update(kw)
    return client.post("/api/returns/suppliers", headers=owner, json=body)


def test_supplier_crud_and_validation(client, owner):
    sup = _supplier(client, owner).json()
    assert sup["name"] == "Армтек" and sup["batches"] == 0
    assert _supplier(client, owner).status_code == 409
    assert _supplier(client, owner, name=" ").status_code == 422
    assert _supplier(client, owner, name="Б", portal_url="javascript:alert(1)").status_code == 422
    r = client.patch(f"/api/returns/suppliers/{sup['id']}", headers=owner, json={"contact": "Новый менеджер"})
    assert r.status_code == 200 and r.json()["contact"] == "Новый менеджер" and r.json()["rules"] == "Одна заявка на счёт"
    assert client.patch(f"/api/returns/suppliers/{sup['id']}", headers=owner, json={"portal_url": "ftp://x"}).status_code == 422
    assert client.get("/api/returns/suppliers").status_code in (401, 403)
    assert client.delete(f"/api/returns/suppliers/{sup['id']}", headers=owner).status_code == 204
    assert client.get("/api/returns/suppliers", headers=owner).json() == []
    assert client.patch(f"/api/returns/suppliers/{sup['id']}", headers=owner, json={"notes": "x"}).status_code == 404


def test_documents_attach_to_supplier(client, owner):
    sup = _supplier(client, owner, name="Другой поставщик").json()
    data = SAMPLE.read_bytes()
    r = client.post(f"/api/returns/import?supplier_id={sup['id']}", headers=owner, files={"file": ("t.csv", data, "text/csv")})
    assert r.status_code == 201, r.text
    s = r.json()
    assert s["supplier_id"] == sup["id"] and s["supplier"] == "Другой поставщик"
    assert [b["id"] for b in client.get(f"/api/returns/batches?supplier_id={sup['id']}", headers=owner).json()] == [s["id"]]
    got = next(x for x in client.get("/api/returns/suppliers", headers=owner).json() if x["id"] == sup["id"])
    assert got["batches"] == 1 and got["positions"] == s["positions"]
    assert client.post("/api/returns/import?supplier_id=9999", headers=owner, files={"file": ("t.csv", data, "text/csv")}).status_code == 404


def test_legacy_import_creates_supplier_by_name(client, owner):
    s = _upload(client, owner, SAMPLE.read_bytes()).json()
    names = [x["name"] for x in client.get("/api/returns/suppliers", headers=owner).json()]
    assert s["supplier"] in names and s["supplier_id"] is not None


def test_weekly_documents_are_not_summed_and_reupload_replaces(client, owner):
    sup = _supplier(client, owner, name="Недели").json()
    data = SAMPLE.read_bytes()
    up = lambda d: client.post(f"/api/returns/import?supplier_id={sup['id']}", headers=owner, files={"file": ("t.csv", d, "text/csv")})
    up(data)
    up(data)  # тот же документ: заменяется, а не дублируется
    assert len(client.get(f"/api/returns/batches?supplier_id={sup['id']}", headers=owner).json()) == 1
    other = data.decode().replace("№000007007", "№000007008").encode()
    assert up(other).status_code == 201  # другая неделя: отдельный документ
    got = next(x for x in client.get("/api/returns/suppliers", headers=owner).json() if x["id"] == sup["id"])
    assert got["batches"] == 2 and got["positions"] == 38  # последний документ, а не 76


def test_period_filter_by_document_date(client, owner):
    sup = _supplier(client, owner, name="Период").json()
    data = SAMPLE.read_bytes().decode()
    up = lambda d: client.post(f"/api/returns/import?supplier_id={sup['id']}", headers=owner, files={"file": ("t.csv", d.encode(), "text/csv")})
    assert up(data).json()["doc_date"] == "2026-11-02"
    assert up(data.replace("№000007007 от 02.11.2026", "№000006900 от 12.10.2026")).json()["doc_date"] == "2026-10-12"
    got = lambda q: [b["doc_date"] for b in client.get(f"/api/returns/batches?supplier_id={sup['id']}{q}", headers=owner).json()]
    assert got("") == ["2026-11-02", "2026-10-12"]
    assert got("&date_from=2026-10-30") == ["2026-11-02"]
    assert got("&date_from=2026-10-01&date_to=2026-10-31") == ["2026-10-12"]
    assert client.get("/api/returns/batches?date_from=bad", headers=owner).status_code == 422


def test_backfill_doc_dates(client, owner):
    from app.db import SessionLocal
    from app.models import ReturnBatch
    from app.routers.returns import backfill_doc_dates

    sup = _supplier(client, owner, name="Старый").json()
    client.post(f"/api/returns/import?supplier_id={sup['id']}", headers=owner, files={"file": ("t.csv", SAMPLE.read_bytes(), "text/csv")})
    with SessionLocal() as db:
        for b in db.query(ReturnBatch).all():
            b.doc_date = None
        db.commit()
    backfill_doc_dates()
    assert client.get("/api/returns/batches", headers=owner).json()[0]["doc_date"] == "2026-11-02"


def _mk(db, sup, number, source="x", statuses=()):
    from app import assembly_returns as ar
    from app.models import ReturnBatch, ReturnLine

    b = ReturnBatch(supplier=sup.name, supplier_id=sup.id, number=number, source=source, doc_date=ar.doc_date_of(number))
    db.add(b)
    db.flush()
    for i, st in enumerate(statuses or ["pending", "pending"]):
        db.add(ReturnLine(batch_id=b.id, article=f"A{i}", qty=1, status=st))
    db.commit()
    return b.id


def test_batch_key_and_dedupe(client, owner):
    from app import assembly_returns as ar
    from app.models import ReturnBatch

    assert ar.batch_key("Сборка №000007007 от 02.11.2026") == ar.batch_key("№000007007 от 02.11.2026") == ar.batch_key("7007 от 02.11.2026")
    assert ar.batch_key("№000007007 от 02.11.2026") != ar.batch_key("№000007007 от 03.11.2026")
    sup = _supplier(client, owner, name="Армтек").json()
    with SessionLocal() as db:
        s = db.get(ReturnSupplier, sup["id"])
        checked = _mk(db, s, "№000007007 от 02.11.2026", statuses=["checked", "checked"])
        dup1 = _mk(db, s, "Сборка №000007007 от 02.11.2026", source="Сборка 1С, файл #18: x")
        dup2 = _mk(db, s, "№000007007 от 02.11.2026")
        other = _mk(db, s, "Сборка №000006891 от 16.10.2026")
        both = _mk(db, s, "000006999 от 01.10.2026", statuses=["checked"])
        both2 = _mk(db, s, "№000006999 от 01.10.2026", statuses=["failed"])
    res = client.post("/api/returns/dedupe", headers=owner)
    assert res.status_code == 200 and res.json() == {"removed": 2, "skipped": 1}
    with SessionLocal() as db:
        ids = {b.id for b in db.query(ReturnBatch).all()}
    assert checked in ids and other in ids and dup1 not in ids and dup2 not in ids
    assert both in ids and both2 in ids  # у обоих есть результаты: сами не удаляем
    assert client.post("/api/returns/dedupe").status_code == 401


def test_reupload_with_other_number_spelling_replaces(client, owner):
    from app import assembly_returns as ar
    from app.models import ReturnBatch

    sup = _supplier(client, owner, name="Армтек").json()
    lines = [{"article": "A1", "name": "n", "brand": "", "qty": 1, "warehouse": "", "invoice_date": "", "invoice_no": "I1", "reason": ""}]
    with SessionLocal() as db:
        s = db.get(ReturnSupplier, sup["id"])
        ar.store_batch(db, s, "Сборка №000007007 от 02.11.2026", lines, "a", 1)
        ar.store_batch(db, s, "№000007007 от 02.11.2026", lines, "b", 1)
        assert db.query(ReturnBatch).count() == 1


def test_check_queue_runs_selected_in_order(client, owner, monkeypatch):
    import time

    from app.models import ReturnRun

    sup = _supplier(client, owner, name="Очередь", portal_url="https://example.com/").json()
    with SessionLocal() as db:
        s = db.get(ReturnSupplier, sup["id"])
        s.session_encrypted = "x"
        db.commit()
        ids = [_mk(db, s, "№000000101 от 01.10.2026"), _mk(db, s, "№000000102 от 02.10.2026"), _mk(db, s, "№000000103 от 03.10.2026")]
    order = []

    def fake(rid):
        with SessionLocal() as db:
            r = db.get(ReturnRun, rid)
            order.append(r.batch_id)
            r.status = "done"
            db.commit()

    monkeypatch.setattr("app.routers.returns.run_check", fake)
    assert client.post("/api/returns/check-queue", headers=owner, json={"batch_ids": []}).status_code == 422
    assert client.post("/api/returns/check-queue", headers=owner, json={"batch_ids": [9999]}).status_code == 404
    r = client.post("/api/returns/check-queue", headers=owner, json={"batch_ids": [ids[2], ids[0]]})
    assert r.status_code == 202
    for _ in range(100):
        st = client.get("/api/returns/check-queue", headers=owner).json()
        if not st["active"]:
            break
        time.sleep(0.05)
    assert order == [ids[2], ids[0]] and [d["status"] for d in st["done"]] == ["done", "done"]
    assert client.post("/api/returns/check-queue", json={"batch_ids": ids}).status_code == 401


def test_queues_of_different_suppliers_run_in_parallel(client, owner, monkeypatch):
    import threading
    import time

    from app.models import ReturnRun

    sups, ids = [], {}
    with SessionLocal() as db:
        pass
    for name in ("Армтек П", "Форум П"):
        sup = _supplier(client, owner, name=name, portal_url="https://example.com/").json()
        with SessionLocal() as db:
            s = db.get(ReturnSupplier, sup["id"])
            s.session_encrypted = "x"
            db.commit()
            ids[sup["id"]] = [_mk(db, s, f"№0000009{len(sups)}1 от 01.10.2026")]
        sups.append(sup["id"])
    both_inside = threading.Barrier(2, timeout=5)

    def fake(rid):
        both_inside.wait()  # пройдёт только если обе проверки идут одновременно
        with SessionLocal() as db:
            db.get(ReturnRun, rid).status = "done"
            db.commit()

    monkeypatch.setattr("app.routers.returns.run_check", fake)
    r = client.post("/api/returns/check-queue", headers=owner, json={"batch_ids": [ids[sups[0]][0], ids[sups[1]][0]]})
    assert r.status_code == 202
    assert sorted(r.json()["active_suppliers"]) == sorted(sups)
    # повторный запуск по тому же поставщику, пока идёт, отклоняется, а по другому разрешён отдельно
    for _ in range(100):
        a = client.get(f"/api/returns/check-queue?supplier_id={sups[0]}", headers=owner).json()
        b = client.get(f"/api/returns/check-queue?supplier_id={sups[1]}", headers=owner).json()
        if not a["active"] and not b["active"]:
            break
        time.sleep(0.05)
    assert [d["status"] for d in a["done"]] == ["done"] and [d["status"] for d in b["done"]] == ["done"]
