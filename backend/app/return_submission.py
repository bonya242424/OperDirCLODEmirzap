"""Подтверждённое оформление возвратов и защита от повторной отправки."""

import json
import re
import threading
from urllib.parse import urlparse

from sqlalchemy import select

from .browser import BrowserError
from .models import ReturnLine, ReturnRun, utcnow

LOCKED_STATUSES = ("submitted", "submitting", "uncertain")
RUN_LOCK = threading.RLock()
ACTIVE_QUEUES = {}
APPROVAL_PREFIX = "Оформление подтверждено: "


def supplier_kind(url):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https":
        return None
    if host == "armtek.ru" or host.endswith(".armtek.ru"):
        return "armtek"
    if host == "forum-auto.ru" or host.endswith(".forum-auto.ru"):
        return "forum"
    from .return_profiles import profile_for
    return "guided" if profile_for(url) else None


def supplier_busy(db, supplier_id):
    from .models import ReturnBatch, ReturnJob

    return ACTIVE_QUEUES.get(supplier_id, {}).get("active") or db.scalar(select(ReturnJob.id).where(ReturnJob.supplier_id == supplier_id, ReturnJob.status.in_(("waiting", "running", "paused"))).limit(1)) or db.scalar(
        select(ReturnRun.id).join(ReturnBatch, ReturnBatch.id == ReturnRun.batch_id)
        .where(ReturnBatch.supplier_id == supplier_id, ReturnRun.status == "running").limit(1)
    )


def eligible_rows(db, batch_id):
    from .models import ReturnBatch
    from .assembly_scope import source_block_reason
    batch = db.get(ReturnBatch, batch_id)
    if batch and source_block_reason(db, batch):
        return []
    return list(db.scalars(select(ReturnLine).where(
        ReturnLine.batch_id == batch_id,
        ReturnLine.status.in_(("pending", "checked", "failed")),
        ReturnLine.request_no == "",
    ).order_by(ReturnLine.id)))


def approval_log(rows, portal_url=None):
    payload = {"lines": [
        {key: getattr(row, key) for key in ("id", "article", "brand", "qty", "invoice_no")}
        for row in rows
    ]}
    if portal_url:
        payload["portal_url"] = portal_url
    return json.dumps([APPROVAL_PREFIX + json.dumps(payload, ensure_ascii=False)], ensure_ascii=False)


def read_approval(run):
    entries = json.loads(run.log or "[]")
    if not entries or not entries[0].startswith(APPROVAL_PREFIX):
        raise BrowserError("Нет подтверждения владельца на оформление")
    return json.loads(entries[0][len(APPROVAL_PREFIX):])


def save_approval(run, approval):
    entries = json.loads(run.log or "[]")
    run.log = json.dumps([APPROVAL_PREFIX + json.dumps(approval, ensure_ascii=False)] + entries[1:], ensure_ascii=False)


def normalized(value):
    return re.sub(r"[^\w]", "", str(value).casefold(), flags=re.UNICODE)


def park_submission(db, run, approval):
    """Изолируем неизвестный результат, сохраняя запрет повторной отправки."""
    attempt = approval.get("attempt")
    if not attempt:
        return False
    for row in db.scalars(select(ReturnLine).where(
        ReturnLine.batch_id == run.batch_id, ReturnLine.id.in_(attempt["ids"]),
        ReturnLine.status.in_(("submitting", "uncertain")),
    )):
        row.status = "uncertain"
        row.note = "Отправка требует сверки в истории поставщика. Повторная отправка и очистка заблокированы."
    approval.setdefault("unresolved_attempts", []).append(approval.pop("attempt"))
    save_approval(run, approval)
    db.commit()
    return True


def validate_preview(preview, rows, host):
    from .return_profiles import profile_for, same_guided_site
    profile = profile_for(preview["url"])
    if urlparse(preview["url"]).netloc != host and not same_guided_site(preview["url"], "https://" + host):
        raise BrowserError("Форма открыта на другом сайте")
    allowed_labels = profile["final"] if profile else ("Оформить возврат", "Отправить заявку")
    if (allowed_labels and preview["label"] not in allowed_labels) or not str(preview.get("label", "")).strip():
        raise BrowserError("Не удалось определить финальную кнопку оформления")
    from .return_brands import effective_brand
    text = normalized(preview["text"])
    if supplier_kind(preview["url"]) == "forum":
        if len(rows) != 1 or "созданиеновоговозврата" not in text:
            raise BrowserError("Форум оформляет одну товарную позицию за заявку")
        row = rows[0]
        if normalized(row.article) not in text or not effective_brand(row) or normalized(effective_brand(row)) not in text:
            raise BrowserError("Товар или бренд в форме не совпадает со сборкой")
        if preview.get("quantities") != [row.qty]:
            raise BrowserError("Количество в форме не совпадает со сборкой")
        if "отказклиента" not in normalized(preview.get("values", "")):
            raise BrowserError("В примечании должен быть указан отказ клиента")
    elif supplier_kind(preview["url"]) == "armtek":
        if "оформитьвозвратпофактуре" not in text:
            raise BrowserError("Сначала откройте итоговое окно оформления возврата")
        if "отказклиента" not in normalized(preview.get("values", "")):
            raise BrowserError("Выберите причину «Отказ клиента»")
        actual = preview.get("items", [])
        expected = {}
        for row in rows:
            key = (normalized(row.article), normalized(effective_brand(row)))
            if not all(key):
                raise BrowserError("Для оформления нужны артикул и бренд")
            expected[key] = expected.get(key, 0) + row.qty
        found = {}
        for item in actual:
            key = (normalized(item["article"]), normalized(item["brand"]))
            found[key] = found.get(key, 0) + item["qty"]
        if found != expected:
            raise BrowserError("Артикулы, бренды или количества в итоговой заявке не совпадают со сборкой")
    elif profile:
        validate_guided_preview(preview, rows, profile)
    else:
        validate_guided_preview(preview, rows, {"reason": ()})



def validate_guided_preview(preview, rows, profile):
    from .return_brands import effective_brand
    from .return_verification import on_page
    if preview.get("root_scope") not in ("form", "dialog") or "возврат" not in normalized(preview.get("text", "")):
        raise BrowserError("Не подтверждена итоговая форма возврата")
    if preview.get("form_valid") is not True or preview.get("unmapped_quantities"):
        raise BrowserError("Не заполнены обязательные поля или не удалось однозначно прочитать количества")
    if len(rows) != 1:
        raise BrowserError("Этот поставщик оформляется по одному текущему товару")
    row = rows[0]
    items = preview.get("items", [])
    if len(items) != 1:
        raise BrowserError("Итоговая форма должна содержать ровно один подтверждённый товар")
    item = items[0]
    if normalized(item.get("article", "")) != normalized(row.article):
        raise BrowserError("Артикул итоговой формы не совпадает с текущим товаром")
    actual_brand = item.get("brand", "")
    brand_ok = normalized(actual_brand) == normalized(effective_brand(row)) if actual_brand else on_page(effective_brand(row), item.get("text", ""))
    if not brand_ok or item.get("qty") != row.qty:
        raise BrowserError("Бренд или количество итоговой формы не совпадают с проверенным товаром")
    values = normalized(preview.get("values", ""))
    if not all(reason in values for reason in profile["reason"]):
        raise BrowserError("Не подтверждена причина возврата и комментарий отказа клиента")


def submission_tool(db, mgr, key, host, batch, run, approval, name, args):
    with RUN_LOCK:
        if run is None or run.mode != "submit" or approval is None:
            raise BrowserError("Отправка доступна только в подтверждённом запуске оформления")
        db.refresh(run)
        if run.status != "running" and name == "submit_return":
            raise BrowserError("Запуск остановлен")
        if name == "record_confirmation":
            return record_guided_confirmation(db, mgr, key, run, approval)
        if name == "submit_return":
            from .assembly_scope import source_block_reason
            if source_block_reason(db, batch):
                raise BrowserError("Отправка запрещена: исходная сборка исключена из-за статуса в 1С")
            ids = args.get("line_ids")
            if not isinstance(ids, list) or not ids or any(type(i) is not int for i in ids) or len(set(ids)) != len(ids):
                raise BrowserError("Укажите уникальные line_id товаров одной заявки")
            if approval.get("attempt"):
                raise BrowserError("Сначала зафиксируйте результат предыдущей отправки")
            rows = list(db.scalars(select(ReturnLine).where(ReturnLine.batch_id == batch.id, ReturnLine.id.in_(ids))))
            allowed = {line["id"]: line for line in approval["lines"]}
            if len(rows) != len(ids):
                raise BrowserError("Товара нет в выбранной сборке")
            for row in rows:
                if row.status in LOCKED_STATUSES or row.request_no:
                    raise BrowserError("Товар уже отправлен либо результат отправки требует сверки")
                if allowed.get(row.id) != {k: getattr(row, k) for k in ("id", "article", "brand", "qty", "invoice_no")}:
                    raise BrowserError("Данные товара изменились после подтверждения")
            if args.get("control"):
                x, y = mgr.control_point(key, str(args["control"]))
            elif "x" in args and "y" in args:
                x, y = float(args["x"]), float(args["y"])
            else:
                raise BrowserError("Укажите control кнопки отправки или её координаты")
            preview = mgr.submission_preview(key, x, y)
            validate_preview(preview, rows, host)
            approval["attempt"] = {"ids": ids, "before": mgr.read_text(key, limit=20000),
                                   "supplier_document": preview.get("supplier_document", ""), "document_url": preview["url"]}
            save_approval(run, approval)
            for row in rows:
                row.status = "submitting"
                row.note = f"Отправка заявки, запуск {run.id}. Повторная отправка заблокирована."
            db.commit()  # Сохраняем попытку до единственного клика.
            try:
                mgr.send_return(key, x, y, preview)
            except Exception:
                for row in rows:
                    row.status = "uncertain"
                    row.note = "Результат отправки неизвестен. Проверьте кабинет поставщика; повторная отправка заблокирована."
                db.commit()
                raise BrowserError("Результат отправки неизвестен. Повторять нажатие нельзя.") from None
            return "Нажатие выполнено один раз. Прочитайте результат и вызовите record_submission с новым номером заявки."

        attempt = approval.get("attempt")
        if not attempt:
            number = str(args.get("request_no", "")).strip()
            if any(receipt.get("request_no") == number for receipt in approval.get("receipts", [])):
                return f"Заявка {number} уже сохранена для предыдущего товара. Повторная запись не нужна; продолжайте текущий товар."
            raise BrowserError("Нет отправки, результат которой нужно зафиксировать")
        number = str(args.get("request_no", "")).strip()
        page = mgr.read_text(key, limit=20000)
        if not 3 <= len(number) <= 100 or not any(c.isdigit() for c in number) or number in attempt["before"] or number not in page:
            raise BrowserError("Новый номер заявки не подтверждён на странице. Сверьте результат отправки, не отправляйте повторно.")
        rows = list(db.scalars(select(ReturnLine).where(ReturnLine.batch_id == batch.id, ReturnLine.id.in_(attempt["ids"]))))
        if any(row.status not in ("submitting", "uncertain") for row in rows):
            raise BrowserError("Попытка уже закрыта или требует ручной сверки")
        for row in rows:
            row.status = "submitted"
            row.request_no = number[:100]
            row.note = f"Возврат оформлен {utcnow().isoformat()}, заявка {number[:100]}, документ поставщика {attempt.get('supplier_document') or 'см. журнал'}, запуск {run.id}"
        approval.setdefault("receipts", []).append({
            "ids": attempt["ids"], "request_no": number, "supplier_document": attempt.get("supplier_document", ""),
            "document_url": attempt.get("document_url", ""), "recorded_at": utcnow().isoformat(),
        })
        approval.pop("attempt")
        save_approval(run, approval)
        db.commit()
        return f"Заявка {number} записана. Эти товары защищены от повторной отправки и очистки."


def record_guided_confirmation(db, mgr, key, run, approval):
    """Сохраняем только новое явное подтверждение поставщика, без выдуманного номера."""
    from .return_profiles import profile_for, confirmed_message
    from .models import ReturnBatch, ReturnSupplier
    attempt = approval.get("attempt")
    if not attempt:
        raise BrowserError("Нет отправки, подтверждение которой нужно сохранить")
    batch = db.get(ReturnBatch, run.batch_id)
    supplier = db.get(ReturnSupplier, batch.supplier_id)
    profile = profile_for(supplier.portal_url)
    if not profile:
        raise BrowserError("Подтверждение без номера недоступно этому поставщику")
    page = mgr.read_text(key, limit=20000)
    proof = confirmed_message(profile, page)
    if not proof or confirmed_message(profile, attempt["before"]):
        raise BrowserError("Новое подтверждение регистрации не показано; повтор отправки запрещён")
    rows = list(db.scalars(select(ReturnLine).where(ReturnLine.batch_id == batch.id, ReturnLine.id.in_(attempt["ids"]))))
    if any(row.status not in ("submitting", "uncertain") for row in rows):
        raise BrowserError("Отправка уже обработана")
    for row in rows:
        row.status = "submitted"
        row.request_no = ""
        row.note = "Возврат зарегистрирован без показанного номера: " + proof[:200]
        evidence = json.loads(row.check_evidence or "{}")
        evidence["submission_confirmation"] = {"run_id": run.id, "at": utcnow().isoformat(), "message": proof[:200]}
        row.check_evidence = json.dumps(evidence, ensure_ascii=False)
    approval.setdefault("receipts", []).append({"ids": attempt["ids"], "confirmation": proof[:200]})
    approval.pop("attempt")
    save_approval(run, approval)
    db.commit()
    return "Регистрация возврата подтверждена поставщиком без номера. Товары защищены от повторной отправки."
