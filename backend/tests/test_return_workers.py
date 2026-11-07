import json
import threading

from app.db import SessionLocal
from app.models import ReturnLine, ReturnRun
from app.return_submission import approval_log, read_approval
from app.return_workers import split_approval
from tests.test_agents import Script
from tests.test_returns_run import _batch, call, no_browser


def test_armtek_workers_are_parallel_and_never_share_an_invoice(client, owner, monkeypatch):
    bid = _batch(client, owner, "https://etp.armtek.ru")
    barrier = threading.Barrier(5, timeout=10)
    assignments = []
    lock = threading.Lock()

    def runner(rid):
        with SessionLocal() as db:
            run = db.get(ReturnRun, rid)
            approval = read_approval(run)
            with lock:
                assignments.append(approval["lines"])
            barrier.wait()  # все пять должны реально работать одновременно
            run.status = "done"
            db.commit()

    monkeypatch.setattr("app.routers.returns.run_check", runner)
    response = client.post(f"/api/returns/batches/{bid}/submit-run", headers=owner, json={"confirmed": True, "workers": 5})
    assert response.status_code == 202
    assert len(assignments) == 5
    ids = [item["id"] for part in assignments for item in part]
    assert len(ids) == len(set(ids))
    invoices = [set(item["invoice_no"] for item in part) for part in assignments]
    assert sum(map(len, invoices)) == len(set.union(*invoices))
    with SessionLocal() as db:
        assert set(ids) == {row.id for row in db.query(ReturnLine).filter_by(batch_id=bid)}
    assert response.json()["worker_total"] == 5


def test_one_invoice_stays_in_one_worker_and_forum_keeps_one(client, owner, monkeypatch):
    bid = _batch(client, owner, "https://itrade.forum-auto.ru")
    with SessionLocal() as db:
        rows = list(db.query(ReturnLine).filter_by(batch_id=bid))
        for row in rows:
            row.invoice_no = "ЮП123 /3"
        rows[-1].invoice_no = "ЮП123"
        parts = split_approval(approval_log(rows), 5)
        assert len(parts) == 1
    calls = []
    monkeypatch.setattr("app.routers.returns.run_check", lambda rid: calls.append(rid))
    assert client.post(f"/api/returns/batches/{bid}/submit-run", headers=owner, json={"confirmed": True, "workers": 5}).status_code == 202
    assert len(calls) == 1
    assert client.post(f"/api/returns/batches/{bid}/submit-run", headers=owner, json={"confirmed": True, "workers": 6}).status_code == 422


def test_worker_finish_does_not_lock_other_workers_submission(client, owner, monkeypatch, no_browser):
    from app.returns_run import run_check
    bid = _batch(client, owner, "https://etp.armtek.ru")
    with SessionLocal() as db:
        rows = list(db.query(ReturnLine).filter_by(batch_id=bid).order_by(ReturnLine.id))
        second_id = rows[1].id
        rows[1].status = "submitting"
        first = ReturnRun(batch_id=bid, mode="submit", log=approval_log(rows[:1]))
        second = ReturnRun(batch_id=bid, mode="submit", log=approval_log(rows[1:2]))
        db.add_all([first, second])
        db.commit()
        rid, other_rid = first.id, second.id
    script = Script(call("finish", reason="blocked", summary="Тест"))
    monkeypatch.setattr("app.returns_run.get_provider", lambda: script)
    run_check(rid)
    with SessionLocal() as db:
        assert db.get(ReturnLine, second_id).status == "submitting"
        assert db.get(ReturnRun, other_rid).status == "running"
        assert "Осталось товарных строк: 1" in db.get(ReturnRun, rid).summary
    assert f"line_id={second_id}:" not in script.seen[0][0][0]["content"]
