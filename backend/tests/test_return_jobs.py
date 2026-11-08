import json
from datetime import timedelta
from types import SimpleNamespace

from app.db import SessionLocal
from app.models import ReturnBatch, ReturnJob, ReturnLine, ReturnRun, ReturnSupplier, utcnow
from app.return_jobs import create_job, pending_ids, run_job, seed_recent_jobs, stop_jobs
from app.return_submission import ACTIVE_QUEUES, approval_log, read_approval, save_approval
from app.returns_run import run_check, tools_for_supplier
from tests.test_agents import Script
from tests.test_returns_run import _batch, call


def make_job(client, owner):
    bid = _batch(client, owner, "https://itrade.forum-auto.ru")
    with SessionLocal() as db:
        batch = db.get(ReturnBatch, bid)
        supplier = db.get(ReturnSupplier, batch.supplier_id)
        rows = list(db.query(ReturnLine).filter_by(batch_id=bid))
        job = create_job(db, supplier, [bid], {bid: approval_log(rows, supplier.portal_url)}, 1, supplier.created_by)
        db.commit()
        return job.id, bid


def complete(rid):
    with SessionLocal() as db:
        run = db.get(ReturnRun, rid)
        for lid in pending_ids(db, run):
            row = db.get(ReturnLine, lid)
            row.status, row.note = "failed", "Товара нет в доступных поставках"
        run.status = "done"
        db.commit()


def test_retry_survives_memory_loss_and_only_runs_remaining(client, owner):
    jid, bid = make_job(client, owner)
    calls = []

    def fail_once(rid):
        calls.append(rid)
        with SessionLocal() as db:
            run = db.get(ReturnRun, rid)
            ids = pending_ids(db, run)
            row = db.get(ReturnLine, ids[0])
            row.status, row.request_no = "submitted", "71822149"
            run.status, run.summary = "failed", "Сбой сервера"
            db.commit()

    run_job(jid, fail_once)
    ACTIVE_QUEUES.clear()  # потеря памяти при перезапуске сервера
    with SessionLocal() as db:
        job = db.get(ReturnJob, jid)
        assert job.status == "waiting" and job.retries == 1 and job.retry_at
        saved_ids = json.loads(job.run_ids)
        assert len(pending_ids(db, db.get(ReturnRun, saved_ids[0]))) > 0
    run_job(jid, complete)
    with SessionLocal() as db:
        assert db.get(ReturnJob, jid).status == "done"
        assert db.query(ReturnRun).filter_by(batch_id=bid).count() == 1
        assert db.query(ReturnLine).filter_by(request_no="71822149").count() == 1


def test_pending_attempt_is_parked_before_recovery(client, owner):
    jid, bid = make_job(client, owner)

    def interrupted(rid):
        with SessionLocal() as db:
            run = db.get(ReturnRun, rid)
            approval = read_approval(run)
            lid = approval["lines"][0]["id"]
            db.get(ReturnLine, lid).status = "submitting"
            approval["attempt"] = {"ids": [lid]}
            save_approval(run, approval)
            run.status = "failed"
            db.commit()

    run_job(jid, interrupted)
    run_job(jid, complete)
    with SessionLocal() as db:
        assert db.query(ReturnLine).filter_by(batch_id=bid, status="uncertain").count() == 1
        assert db.get(ReturnJob, jid).status == "done"


def test_stop_is_durable_and_prevents_recovery(client, owner):
    jid, _ = make_job(client, owner)
    with SessionLocal() as db:
        job = db.get(ReturnJob, jid)
        stop_jobs(db, job.supplier_id)
        db.commit()
    ACTIVE_QUEUES.clear()
    run_job(jid, lambda _: (_ for _ in ()).throw(AssertionError("stopped job restarted")))
    with SessionLocal() as db:
        assert db.get(ReturnJob, jid).status == "stopped"


def test_portal_change_pauses_without_sending(client, owner):
    jid, _ = make_job(client, owner)
    with SessionLocal() as db:
        job = db.get(ReturnJob, jid)
        db.get(ReturnSupplier, job.supplier_id).portal_url = "https://etp.armtek.ru"
        db.commit()
    run_job(jid, lambda _: (_ for _ in ()).throw(AssertionError("wrong supplier")))
    with SessionLocal() as db:
        assert db.get(ReturnJob, jid).status == "paused"


def test_all_selected_batches_continue_after_local_failure(client, owner):
    jid, bid = make_job(client, owner)
    with SessionLocal() as db:
        job = db.get(ReturnJob, jid)
        payload = json.loads(job.payload)
        second = ReturnBatch(supplier_id=job.supplier_id, supplier="Форум", number="second")
        db.add(second)
        db.flush()
        row = ReturnLine(batch_id=second.id, article="SECOND", brand="TEST", qty=1)
        db.add(row)
        db.flush()
        payload["batch_ids"].append(second.id)
        payload["approvals"][str(second.id)] = approval_log([row], payload["portal_url"])
        job.payload = json.dumps(payload)
        db.commit()
    run_job(jid, complete)
    with SessionLocal() as db:
        job = db.get(ReturnJob, jid)
        assert job.status == "done" and job.position == 2
        assert len(json.loads(job.payload)["done"]) == 2


def test_supplier_tools_and_receipt_phase_are_isolated():
    for kind, forbidden in (("forum", "armtek_"), ("armtek", "forum_")):
        names = {tool["name"] for tool in tools_for_supplier(kind, True)}
        assert not any(name.startswith(forbidden) for name in names)
        assert "submit_return" in names and "record_submission" not in names
        awaiting = {tool["name"] for tool in tools_for_supplier(kind, True, True)}
        assert "record_submission" in awaiting and "submit_return" not in awaiting


def test_model_proxy_recovery_rotates_then_keeps_working_proxy():
    from app.llm import FailoverProvider, LLMConnectionError
    calls = []

    def make(url):
        def step(*args):
            calls.append(url)
            if url != "recovery-test-good":
                raise LLMConnectionError("timeout")
            return "ok"
        return SimpleNamespace(step=step, interactive_limits=lambda: None)

    proxies = [(991, "recovery-test-bad1"), (992, "recovery-test-bad2"), (993, "recovery-test-good")]
    first = FailoverProvider(proxies, lambda *args: None, "test", make)
    first.interactive_limits()
    import pytest
    with pytest.raises(LLMConnectionError):
        first.step("", [], [])
    second = FailoverProvider(proxies, lambda *args: None, "test", make)
    second.interactive_limits()
    assert second.step("", [], []) == "ok"
    assert second.step("", [], []) == "ok"
    assert calls == ["recovery-test-bad1", "recovery-test-bad2", "recovery-test-good", "recovery-test-good"]


def test_scheduler_waits_for_new_login_and_does_not_revive_manual_stop(client, owner, monkeypatch):
    from app.return_jobs import recover_jobs
    jid, _ = make_job(client, owner)
    with SessionLocal() as db:
        job = db.get(ReturnJob, jid)
        supplier = db.get(ReturnSupplier, job.supplier_id)
        supplier.session_saved_at = utcnow()
        payload = json.loads(job.payload)
        payload["session_saved_at"] = supplier.session_saved_at.isoformat()
        job.payload = json.dumps(payload)
        job.status, job.note = "paused", "Нужен вход в кабинет"
        db.commit()
    launches = []
    monkeypatch.setattr("app.return_jobs.threading.Thread", lambda **kwargs: SimpleNamespace(start=lambda: launches.append(kwargs["args"][0])))
    recover_jobs()
    assert not launches
    with SessionLocal() as db:
        job = db.get(ReturnJob, jid)
        db.get(ReturnSupplier, job.supplier_id).session_saved_at = utcnow() + timedelta(seconds=1)
        db.commit()
    recover_jobs()
    assert launches == [jid]
    with SessionLocal() as db:
        stop_jobs(db, db.get(ReturnJob, jid).supplier_id)
        db.commit()
    recover_jobs()
    assert launches == [jid]


def test_upgrade_recovers_only_approved_unfinished_items(client, owner):
    from app.models import AppSetting
    from app.return_jobs import SEED_KEY
    jid, bid = make_job(client, owner)
    with SessionLocal() as db:
        job = db.get(ReturnJob, jid)
        payload = json.loads(job.payload)
        run = ReturnRun(batch_id=bid, mode="submit", created_by=job.created_by, status="failed", log=payload["approvals"][str(bid)])
        db.add(run)
        rows = list(db.query(ReturnLine).filter_by(batch_id=bid).order_by(ReturnLine.id))
        rows[0].status, rows[0].request_no = "submitted", "71822149"
        rows[1].status = "uncertain"
        rows[2].status, rows[2].note = "failed", "не удалось проверить: ошибка сервера"
        db.delete(job)
        db.delete(db.get(AppSetting, SEED_KEY))
        db.commit()
    seed_recent_jobs()
    with SessionLocal() as db:
        jobs = list(db.query(ReturnJob))
        assert len(jobs) == 1
        approval = json.loads(json.loads(jobs[0].payload)["approvals"][str(bid)])[0]
        from app.return_submission import APPROVAL_PREFIX
        ids = [item["id"] for item in json.loads(approval[len(APPROVAL_PREFIX):])["lines"]]
        assert rows[0].id not in ids and rows[1].id not in ids and rows[2].id in ids
    seed_recent_jobs()
    with SessionLocal() as db:
        assert db.query(ReturnJob).count() == 1


def test_repeated_blocked_item_is_skipped_and_next_is_processed(client, owner, monkeypatch):
    jid, bid = make_job(client, owner)
    with SessionLocal() as db:
        rows = list(db.query(ReturnLine).filter_by(batch_id=bid).order_by(ReturnLine.id))
        ids = [r.id for r in rows]
        # две строки достаточно для проверки перехода
        db.query(ReturnLine).filter(ReturnLine.batch_id == bid, ReturnLine.id.not_in(ids[:2])).delete()
        supplier = db.get(ReturnSupplier, db.get(ReturnBatch, bid).supplier_id)
        run = ReturnRun(batch_id=bid, mode="submit", log=approval_log(rows[:2], supplier.portal_url))
        db.add(run)
        db.commit()
        rid = run.id
    visits = []
    browser = SimpleNamespace(open=lambda *a: None, close=lambda *a: None, screenshot=lambda *a: b"image",
        read_text=lambda *a, **kw: "Страница поиска", auth_required=lambda *a: False,
        navigate=lambda *a: visits.append(a[-1]))
    script = Script(call("finish", reason="blocked", summary="Старая заявка другого товара"),
        call("finish", reason="blocked", summary="Не удалось перейти"),
        call("finish", reason="blocked", summary="Повторная попытка также не помогла"),
        call("record_item_result", line_id=ids[1], status="error", note="Нет доступных поставок"))
    monkeypatch.setattr("app.returns_run.manager", browser)
    monkeypatch.setattr("app.returns_run.get_provider", lambda: script)
    run_check(rid)
    with SessionLocal() as db:
        assert db.get(ReturnLine, ids[0]).status == "failed"
        assert "пропущен" in db.get(ReturnLine, ids[0]).note
        assert db.get(ReturnLine, ids[1]).status == "failed"
        assert not pending_ids(db, db.get(ReturnRun, rid))
    assert len(visits) == 2
    assert all(url.endswith("/shop/returns/index.html") for url in visits)
    assert f"line_id={ids[1]}:" in script.seen[-1][0][0]["content"]


def test_paused_login_queue_has_login_status_and_allows_manual_retry(client, owner, monkeypatch):
    from app import connections
    jid, bid = make_job(client, owner)

    def needs_login(rid):
        with SessionLocal() as db:
            run = db.get(ReturnRun, rid)
            run.status, run.summary = "failed", "Нужен вход: предыдущий автовход не подтверждён"
            db.commit()

    run_job(jid, needs_login)
    with SessionLocal() as db:
        job = db.get(ReturnJob, jid)
        sid = job.supplier_id
        supplier = db.get(ReturnSupplier, sid)
        supplier.credentials_encrypted = connections.encrypt({"username": "test", "password": "test"})
        supplier.login_status = "error"
        db.commit()
    suppliers = client.get("/api/returns/suppliers", headers=owner).json()
    assert next(s for s in suppliers if s["id"] == sid)["check_status"] == "needs_login"
    queue = client.get(f"/api/returns/check-queue?supplier_id={sid}", headers=owner).json()
    assert queue["status"] == "paused"
    assert sid not in queue["active_suppliers"]
    calls = []
    monkeypatch.setattr("app.supplier_credentials.preflight_one", lambda supplier_id: calls.append(supplier_id))
    response = client.post(f"/api/returns/suppliers/{sid}/auto-login", headers=owner)
    assert response.status_code == 202, response.text
    assert calls == [sid]
    with SessionLocal() as db:
        assert db.get(ReturnSupplier, sid).login_status == "checking"
        assert db.get(ReturnJob, jid).status == "paused"


def test_repeated_technical_failure_pauses_instead_of_retrying_forever(client, owner, monkeypatch):
    from app.return_jobs import recover_jobs
    jid, bid = make_job(client, owner)

    def fail(rid):
        with SessionLocal() as db:
            run = db.get(ReturnRun, rid)
            run.status, run.summary = "failed", "Модель не ответила"
            db.commit()

    for _ in range(4):
        run_job(jid, fail)
    with SessionLocal() as db:
        job = db.get(ReturnJob, jid)
        assert job.status == "paused" and job.retry_at is None
        assert "трёх повторных попыток" in job.note
        sid = job.supplier_id
    ACTIVE_QUEUES.clear()
    launches = []
    monkeypatch.setattr("app.return_jobs.threading.Thread",
                        lambda **kwargs: SimpleNamespace(start=lambda: launches.append(kwargs["args"][0])))
    recover_jobs()
    assert launches == []
    suppliers = client.get("/api/returns/suppliers", headers=owner).json()
    assert next(s for s in suppliers if s["id"] == sid)["check_status"] == "paused"


def test_manual_auto_login_cannot_interrupt_running_job(client, owner, monkeypatch):
    from app import connections
    jid, _ = make_job(client, owner)
    with SessionLocal() as db:
        job = db.get(ReturnJob, jid)
        sid = job.supplier_id
        job.status = "running"
        supplier = db.get(ReturnSupplier, sid)
        supplier.credentials_encrypted = connections.encrypt({"username": "test", "password": "test"})
        db.commit()
    calls = []
    monkeypatch.setattr("app.supplier_credentials.preflight_one", lambda supplier_id: calls.append(supplier_id))
    response = client.post(f"/api/returns/suppliers/{sid}/auto-login", headers=owner)
    assert response.status_code == 409
    assert calls == []
