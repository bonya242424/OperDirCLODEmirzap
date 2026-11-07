"""Результат проверки относится только к одной подтверждённой товарной строке."""

import json
import logging
import re

from sqlalchemy import case, select

from .browser import BrowserError
from .models import AppSetting, ReturnLine, utcnow
from .return_submission import LOCKED_STATUSES, normalized

MIGRATION_KEY = "return-item-evidence-v1"
log = logging.getLogger(__name__)


def on_page(value, page):
    token = normalized(value)
    if not token:
        return False
    pattern = r"(?<!\w)" + r"[\s./-]*".join(re.escape(c) for c in token) + r"(?!\w)"
    return bool(re.search(pattern, page, re.I))


def validate_item(db, mgr, key, batch, args, submit=False, line_ids=None):
    if line_ids == []:
        raise BrowserError("Нет подтверждённых товаров для проверки")
    line_id = args.get("line_id")
    if type(line_id) is not int:
        raise BrowserError("Укажите числовой line_id одной товарной строки")
    row = db.scalar(select(ReturnLine).where(ReturnLine.batch_id == batch.id, ReturnLine.id == line_id))
    if row is None:
        raise BrowserError("Строки нет в выбранной сборке")
    if row.status in LOCKED_STATUSES or row.request_no:
        raise BrowserError("Результат отправки защищён от изменения")
    query = select(ReturnLine.id).where(ReturnLine.batch_id == batch.id,
        ReturnLine.status.in_(("pending", "checked")) if submit else ReturnLine.status == "pending", ReturnLine.request_no == "")
    if line_ids is not None:
        query = query.where(ReturnLine.id.in_(line_ids)).order_by(case({lid: index for index, lid in enumerate(line_ids)}, value=ReturnLine.id))
    else:
        query = query.order_by(ReturnLine.id)
    current_id = db.scalar(query.limit(1))
    if row.id != current_id:
        raise BrowserError("Результат относится не к текущему товару. Сначала завершите его обработку.")
    if normalized(args.get("article", "")) != normalized(row.article) or normalized(args.get("brand", "")) != normalized(row.brand):
        raise BrowserError("Артикул или бренд результата не совпадает с товарной строкой. Проверьте текущий товар.")
    status = args.get("status")
    if status in ("ok", "already_returned", "mismatch"):
        page = mgr.read_text(key, limit=20000)
        if not on_page(row.article, page) or not on_page(row.brand, page):
            raise BrowserError("На странице не подтверждено сочетание артикула и бренда текущего товара")
    available = args.get("available_qty")
    if status in ("ok", "mismatch"):
        if type(available) is not int or available < 0:
            raise BrowserError("Укажите доступное количество этого товара на сайте")
        if status == "ok" and available < row.qty:
            raise BrowserError("Доступно меньше количества из сборки: результат не может быть «можно оформить»")
        if status == "mismatch" and available >= row.qty:
            raise BrowserError("Количество достаточно: нельзя записывать расхождение")
    return row


def invalidate_legacy(db):
    """Старые проверки без привязки к артикулу перепроверяются, отправки не меняются."""
    if db.get(AppSetting, MIGRATION_KEY):
        return
    count = 0
    for row in db.scalars(select(ReturnLine).where(
        ReturnLine.status.in_(("checked", "failed")), ReturnLine.request_no == "", ReturnLine.check_evidence == "",
    )):
        row.check_evidence = json.dumps({"version": 0, "legacy_status": row.status, "legacy_note": row.note,
                                         "invalidated_at": utcnow().isoformat()}, ensure_ascii=False)
        row.status = "pending"
        row.note = "Предыдущая проверка не имеет подтверждённой привязки к товару. Требуется новая проверка по артикулу и бренду."
        count += 1
    db.add(AppSetting(key=MIGRATION_KEY, value=str(count)))
    db.commit()
    log.info("Возвращены на проверку результаты без привязки к товару: %s", count)
