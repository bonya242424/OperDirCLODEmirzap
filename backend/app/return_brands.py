"""Выбор единственного бренда по точному артикулу; исходная строка 1С неизменна."""
import json
import re

BRAND_RULE = (
    "Общее правило брендов для всех поставщиков: ищи по полному артикулу, игнорируя пробелы, точки, дефисы и слеши. "
    "Если для этого точного артикула на сайте есть только один вариант бренда, используй его даже при отличии названия от 1С "
    "(например, KOLBENSCHMIDT / KS ФИЛЬТРА или GKN / LOBRO). "
    "Если вариантов бренда несколько, выбирай соответствующий бренду сборки; при неоднозначности не отправляй. "
    "В журнале фиксируй название из сборки и принятое название сайта. "
    "Проверь исходную поставку, дату, доступное количество и отсутствие повторной отправки. "
    "После подтверждения сразу оформляй возврат текущего товара. "
    "Это правило имеет приоритет над прежними требованиями буквального совпадения бренда в инструкции."
)
MARKER = "Общее правило брендов для всех поставщиков:"


def norm(value):
    return re.sub(r"[^\w]", "", str(value).casefold(), flags=re.UNICODE)


def selected_indices(candidates, article, brand):
    """Рассматриваем все варианты точного артикула, включая недоступные."""
    relevant = [(index, item) for index, item in enumerate(candidates)
                if norm(item.get("article", "")) == norm(article) and item.get("brand", "").strip()]
    exact = [index for index, item in relevant if norm(item["brand"]) == norm(brand)]
    if exact:
        return exact
    brands = {norm(item["brand"]) for _, item in relevant}
    return [index for index, _ in relevant] if len(brands) == 1 else []


def product_brand(product, article):
    # Форум печатает артикул + наименование бренда и товара в одной ячейке.
    # Без отдельного поля бренда считаем различающиеся полные варианты неоднозначными.
    token = norm(article)
    if not token:
        return ""
    pattern = r"^\s*" + r"[\s./-]*".join(re.escape(char) for char in token) + r"(?!\w)"
    match = re.match(pattern, product, re.I)
    return product[match.end():].strip() if match else ""


def remember_selection(row, actual_brand):
    evidence = json.loads(getattr(row, "check_evidence", "") or "{}")
    evidence["brand_selection"] = {
        "article": row.article, "brand": row.brand, "qty": row.qty,
        "invoice_no": row.invoice_no, "actual_brand": actual_brand,
    }
    row.check_evidence = json.dumps(evidence, ensure_ascii=False)


def effective_brand(row):
    evidence = json.loads(getattr(row, "check_evidence", "") or "{}").get("brand_selection", {})
    if all(evidence.get(key) == getattr(row, key, None) for key in ("article", "brand", "qty", "invoice_no")):
        return evidence.get("actual_brand") or row.brand
    return row.brand


def instruction_text(text):
    return text if MARKER in (text or "") else BRAND_RULE + "\n\n" + (text or "")


def sync_instructions(db, supplier_id=None):
    from sqlalchemy import select
    from .models import ReturnSupplier, ReturnGuide
    query = select(ReturnSupplier)
    if supplier_id is not None:
        query = query.where(ReturnSupplier.id == supplier_id)
    for supplier in db.scalars(query):
        supplier.rules = instruction_text(supplier.rules)
        guides = list(db.scalars(select(ReturnGuide).where(ReturnGuide.supplier_id == supplier.id)))
        for guide in guides:
            if guide.text:
                guide.text = instruction_text(guide.text)
        if not guides:
            db.add(ReturnGuide(supplier_id=supplier.id, filename="Общие правила возвратов", disk_name="",
                size=0, status="ok", text=BRAND_RULE, created_by=supplier.created_by,
                message="Общее правило выбора бренда. Дополните шагами кабинета поставщика."))
