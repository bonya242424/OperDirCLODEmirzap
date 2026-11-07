"""Независимые потоки Армтека: одна фактура всегда закреплена за одним потоком."""

import json
import logging
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select

from .db import SessionLocal
from .browser import BrowserError
from .models import ReturnLine, ReturnRun, utcnow
from .return_submission import APPROVAL_PREFIX, normalized, read_approval

log = logging.getLogger(__name__)


def split_approval(log, workers):
    if workers == 1:
        return [log]
    entries = json.loads(log)
    approval = json.loads(entries[0][len(APPROVAL_PREFIX):])
    groups = defaultdict(list)
    for item in approval["lines"]:
        invoice = normalized(re.sub(r"/\s*\d+\s*$", "", item["invoice_no"]))
        groups[invoice].append(item)
    if not groups:
        return [log]
    buckets = [[] for _ in range(min(workers, len(groups)))]
    for group in sorted(groups.values(), key=len, reverse=True):
        min(buckets, key=len).extend(group)
    return [json.dumps([APPROVAL_PREFIX + json.dumps({"lines": bucket,
        "worker": {"index": index + 1, "total": len(buckets)}}, ensure_ascii=False)], ensure_ascii=False)
        for index, bucket in enumerate(buckets)]


def execute_runs(ids, runner):
    def execute(rid):
        try:
            runner(rid)
        except Exception:
            log.exception("Поток возвратов %s завершился с ошибкой", rid)
            with SessionLocal() as db:
                run = db.get(ReturnRun, rid)
                if run is None:
                    return
                try:
                    line_ids = [item["id"] for item in read_approval(run)["lines"]]
                except (BrowserError, ValueError, KeyError, IndexError):
                    line_ids = []
                for row in db.scalars(select(ReturnLine).where(ReturnLine.batch_id == run.batch_id, ReturnLine.id.in_(line_ids), ReturnLine.status == "submitting")):
                    row.status, row.note = "uncertain", "Поток прерван во время отправки. Результат требует сверки; повтор запрещён."
                run.status, run.summary, run.finished_at = ("stopped" if run.status == "stopped" else "failed"), "Поток возвратов завершился с ошибкой. Другие потоки продолжают работу.", utcnow()
                db.commit()
    if len(ids) == 1:
        return execute(ids[0])
    with ThreadPoolExecutor(max_workers=min(5, len(ids)), thread_name_prefix="return-worker") as pool:
        list(pool.map(execute, ids))
