"""Долговечная очередь: не расширяет подтверждение и не повторяет неизвестную отправку."""

import json
import logging
import threading
from datetime import timedelta, timezone

from sqlalchemy import select

from .browser import BrowserError
from .db import SessionLocal
from .models import AppSetting, ReturnBatch, ReturnJob, ReturnLine, ReturnRun, ReturnSupplier, User, utcnow
from .return_submission import ACTIVE_QUEUES, RUN_LOCK, approval_log, park_submission, read_approval, supplier_kind
from .return_workers import execute_runs, split_approval

ACTIVE_STATUSES = ("waiting", "running", "paused")
ACTIVE_JOBS = set()
SEED_KEY = "return-jobs-recovery-v1"
log = logging.getLogger(__name__)


def session_stamp(supplier):
    saved = supplier.session_saved_at if supplier else None
    return saved.replace(tzinfo=timezone.utc).isoformat() if saved else ""


def create_job(db, supplier, batch_ids, approvals, workers, owner_id, run_ids=None):
    job = ReturnJob(supplier_id=supplier.id, created_by=owner_id, payload=json.dumps({
        "batch_ids": batch_ids, "approvals": {str(k): v for k, v in approvals.items()}, "workers": workers,
        "portal_url": supplier.portal_url, "session_saved_at": session_stamp(supplier), "done": [],
    }, ensure_ascii=False), run_ids=json.dumps(run_ids or []), note="Очередь сохранена; автоматическое восстановление включено")
    db.add(job)
    db.flush()
    return job


def queue_snapshot(job):
    payload = json.loads(job.payload)
    ids = payload["batch_ids"]
    return {"job_id": job.id, "active": job.status in ACTIVE_STATUSES, "cancel": job.status == "stopped",
            "ids": ids, "pos": min(job.position + 1, len(ids)), "done": payload.get("done", []), "note": job.note,
            "mode": "submit", "workers": payload["workers"]}


def load_queue(db, supplier_id):
    job = db.scalar(select(ReturnJob).where(ReturnJob.supplier_id == supplier_id).order_by(ReturnJob.id.desc()).limit(1))
    if job:
        current = ACTIVE_QUEUES.get(supplier_id)
        if not current or current.get("job_id") == job.id or job.status in ACTIVE_STATUSES:
            ACTIVE_QUEUES[supplier_id] = queue_snapshot(job)
    return ACTIVE_QUEUES.get(supplier_id)


def stop_jobs(db, supplier_id):
    for job in db.scalars(select(ReturnJob).where(ReturnJob.supplier_id == supplier_id, ReturnJob.status.in_(ACTIVE_STATUSES))):
        job.status, job.note, job.updated_at = "stopped", "Остановлено владельцем; автоматический запуск отменён", utcnow()
        for rid in json.loads(job.run_ids):
            run = db.get(ReturnRun, rid)
            if run and run.status == "running":
                run.status = "stopped"
        ACTIVE_QUEUES[supplier_id] = queue_snapshot(job)


def pending_ids(db, run):
    approval = read_approval(run)
    expected = {item["id"]: item for item in approval["lines"]}
    ids = []
    for row in db.scalars(select(ReturnLine).where(ReturnLine.batch_id == run.batch_id, ReturnLine.id.in_(expected),
                                                   ReturnLine.status.in_(("pending", "checked")), ReturnLine.request_no == "")):
        if expected[row.id] != {k: getattr(row, k) for k in ("id", "article", "brand", "qty", "invoice_no")}:
            row.status, row.note = "failed", "Данные товара изменены после подтверждения; автоматическая отправка запрещена"
        else:
            ids.append(row.id)
    return ids


def retry_later(job, note):
    job.retries += 1
    delay = min(300, 10 * 2 ** min(job.retries - 1, 5))
    job.status, job.retry_at, job.updated_at = "waiting", utcnow() + timedelta(seconds=delay), utcnow()
    job.note = f"Автовосстановление через {delay} с. {note}"[:1000]


def run_job(job_id, runner=None):
    with RUN_LOCK:
        if job_id in ACTIVE_JOBS:
            return
        ACTIVE_JOBS.add(job_id)
    try:
        if runner is None:
            from .returns_run import run_check
            runner = run_check
        with SessionLocal() as db:
            job = db.get(ReturnJob, job_id)
            if job is None or job.status not in ACTIVE_STATUSES:
                return
            supplier = db.get(ReturnSupplier, job.supplier_id)
            payload = json.loads(job.payload)
            if not supplier or not supplier.is_active or supplier.portal_url != payload["portal_url"] or not supplier_kind(supplier.portal_url):
                job.status, job.note = "paused", "Изменён поставщик или адрес кабинета. Нужен новый подтверждённый запуск."
                db.commit()
                return
            if not supplier.session_encrypted and not supplier.credentials_encrypted:
                job.status, job.note = "paused", "Нужен вход в кабинет поставщика; очередь сохранена"
                db.commit()
                return
            session_used = session_stamp(supplier)
            job.status, job.note, job.updated_at = "running", "Очередь выполняется, автоматическое восстановление включено", utcnow()
            db.commit()
            while job.position < len(payload["batch_ids"]):
                db.refresh(job)
                if job.status == "stopped":
                    return
                db.refresh(supplier)
                if supplier.portal_url != payload["portal_url"] or not supplier.is_active:
                    job.status, job.note = "paused", "Изменён адрес кабинета; нужен новый запуск"
                    db.commit()
                    return
                bid = payload["batch_ids"][job.position]
                if db.get(ReturnBatch, bid) is None:
                    job.status, job.note = "paused", "Подтверждённая сборка удалена; нужен новый запуск"
                    db.commit()
                    return
                ids = json.loads(job.run_ids)
                if not ids:
                    parts = split_approval(payload["approvals"][str(bid)], payload["workers"])
                    runs = [ReturnRun(batch_id=bid, mode="submit", created_by=job.created_by, log=part) for part in parts]
                    db.add_all(runs)
                    db.flush()
                    ids = [run.id for run in runs]
                    job.run_ids = json.dumps(ids)
                    db.commit()  # запуск сохранён до начала браузерных действий
                active = []
                for rid in ids:
                    run = db.get(ReturnRun, rid)
                    if run is None or run.status == "stopped":
                        job.status, job.note = "stopped", "Запуск остановлен владельцем"
                        db.commit()
                        return
                    approval = read_approval(run)
                    if approval.get("attempt"):
                        park_submission(db, run, approval)
                    if pending_ids(db, run):
                        run.status, run.finished_at = "running", None
                        active.append(rid)
                db.commit()
                ACTIVE_QUEUES[job.supplier_id] = queue_snapshot(job)
                if active:
                    execute_runs(active, runner)
                db.expire_all()
                job = db.get(ReturnJob, job_id)
                if job.status == "stopped":
                    return
                runs = [db.get(ReturnRun, rid) for rid in ids]
                if any(run.status == "stopped" for run in runs):
                    job.status, job.note = "stopped", "Остановлено владельцем"
                    db.commit()
                    return
                if any(pending_ids(db, run) for run in runs):
                    if any("Нужен вход" in run.summary for run in runs):
                        job.status, job.note = "paused", "Нужен вход в кабинет поставщика; очередь сохранена"
                        payload["session_saved_at"] = session_used
                        job.payload = json.dumps(payload, ensure_ascii=False)
                    else:
                        retry_later(job, "Временный сбой запуска; необработанные товары будут продолжены")
                    db.commit()
                    return
                payload.setdefault("done", []).append({"batch_id": bid, "status": "done" if all(run.status == "done" for run in runs) else "failed"})
                job.position += 1
                job.run_ids, job.retries, job.retry_at = "[]", 0, None
                job.payload, job.updated_at = json.dumps(payload, ensure_ascii=False), utcnow()
                db.commit()
            job.status, job.note, job.updated_at = "done", "Все подтверждённые сборки пройдены. Результат сохранён по каждому товару.", utcnow()
            db.commit()
    except Exception:
        log.exception("Восстановление очереди возвратов %s", job_id)
        with SessionLocal() as db:
            job = db.get(ReturnJob, job_id)
            if job and job.status != "stopped":
                retry_later(job, "Ошибка сервера; прогресс сохранён")
                db.commit()
    finally:
        try:
            with SessionLocal() as db:
                job = db.get(ReturnJob, job_id)
                if job:
                    ACTIVE_QUEUES[job.supplier_id] = queue_snapshot(job)
        finally:
            with RUN_LOCK:
                ACTIVE_JOBS.discard(job_id)


def recover_jobs():
    """Вызывается планировщиком; один процесс приложения, один исполнитель задания."""
    with SessionLocal() as db:
        from .supplier_credentials import launch_approved
        launch_approved(db)
        now = utcnow()
        for job in db.scalars(select(ReturnJob).where(ReturnJob.status.in_(ACTIVE_STATUSES))):
            ACTIVE_QUEUES[job.supplier_id] = queue_snapshot(job)
            if job.id in ACTIVE_JOBS:
                continue
            if job.status == "paused":
                if not job.note.startswith("Нужен вход"):
                    continue
                supplier = db.get(ReturnSupplier, job.supplier_id)
                payload = json.loads(job.payload)
                saved = session_stamp(supplier)
                if not supplier or supplier.portal_url != payload["portal_url"]:
                    continue
                if (not saved or saved == payload.get("session_saved_at")) and not (supplier.credentials_encrypted and supplier.login_status in ("ready", "ok")):
                    continue
            if job.retry_at and job.retry_at.replace(tzinfo=timezone.utc) > now:
                continue
            threading.Thread(target=run_job, args=(job.id,), daemon=True, name=f"returns-job-{job.id}").start()


def seed_recent_jobs(seed_key=SEED_KEY, armtek_inputs=False):
    """Продолжает только недавно подтверждённые, незавершённые запуски; остановленные не воскрешает."""
    with SessionLocal() as db:
        if db.get(AppSetting, seed_key):
            return
        batches = list(db.scalars(select(ReturnBatch)))
        grouped = {}
        for batch in batches:
            runs = list(db.scalars(select(ReturnRun).where(ReturnRun.batch_id == batch.id, ReturnRun.mode == "submit",
                ReturnRun.created_at >= utcnow() - timedelta(days=1)).order_by(ReturnRun.id.desc())))
            if not runs:
                continue
            latest = runs[0]
            cohort = [run for run in runs if abs((latest.created_at - run.created_at).total_seconds()) < 2]
            if any(run.status == "stopped" for run in cohort):
                continue
            supplier = db.get(ReturnSupplier, batch.supplier_id)
            owner = db.get(User, latest.created_by) if latest.created_by else None
            if not supplier or not supplier.is_active or not supplier_kind(supplier.portal_url) or not owner or owner.role != "owner":
                continue
            if armtek_inputs:
                last_job = db.scalar(select(ReturnJob).where(ReturnJob.supplier_id == supplier.id).order_by(ReturnJob.id.desc()).limit(1))
                if supplier_kind(supplier.portal_url) != "armtek" or (last_job and last_job.status == "stopped"):
                    continue
                if not any(any(marker in entry for marker in ("Выберите поле поиска артикула", "Подсказки Армтека: не появились")) for run in cohort for entry in json.loads(run.log or "[]")):
                    continue
            approved = {}
            workers = 1
            for run in reversed(cohort):
                try:
                    data = read_approval(run)
                except (BrowserError, ValueError, KeyError):
                    continue
                workers = max(workers, data.get("worker", {}).get("total", 1))
                for item in data["lines"]:
                    approved[item["id"]] = item
            rows = []
            for row in db.scalars(select(ReturnLine).where(ReturnLine.batch_id == batch.id, ReturnLine.id.in_(approved), ReturnLine.request_no == "")):
                if approved[row.id] != {k: getattr(row, k) for k in ("id", "article", "brand", "qty", "invoice_no")}:
                    continue
                if armtek_inputs and row.status == "failed":
                    evidence = json.loads(row.check_evidence or "{}")
                    if not evidence.get("technical_skip") and not (evidence.get("searches") and "подсказк" in row.note.casefold()):
                        continue
                    evidence.update(automation_restarts=0, technical_skip=False, matnr_repaired=True)
                    row.check_evidence = json.dumps(evidence, ensure_ascii=False)
                    row.status, row.note = "pending", "Повтор после исправления поиска MATNR"
                if not armtek_inputs and row.status == "failed" and row.note.startswith("не удалось проверить:"):
                    row.status = "pending"
                if row.status in ("pending", "checked"):
                    rows.append(row)
            if rows:
                group = grouped.setdefault(supplier.id, {"supplier": supplier, "ids": [], "approvals": {}, "workers": workers, "owner": owner.id})
                group["ids"].append(batch.id)
                group["approvals"][batch.id] = approval_log(rows, supplier.portal_url)
        for group in grouped.values():
            if not db.scalar(select(ReturnJob.id).where(ReturnJob.supplier_id == group["supplier"].id, ReturnJob.status.in_(ACTIVE_STATUSES))):
                create_job(db, group["supplier"], group["ids"], group["approvals"], group["workers"], group["owner"])
        db.add(AppSetting(key=seed_key, value="done"))
        db.commit()
