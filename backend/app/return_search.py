"""Варианты артикула для поиска без подмены товара и бренда."""

import re
import math

from .return_verification import on_page


def article_variants(article, brand):
    compact = re.sub(r"[\s./-]+", "", article).upper()
    values = [article.strip(), compact]
    if brand.upper().strip() in ("VAG", "VW", "VOLKSWAGEN", "AUDI", "SKODA", "SEAT", "PORSCHE") and 9 < len(compact) <= 12:
        values += [compact[:9] + " " + compact[9:], " ".join([compact[:3], compact[3:6], compact[6:9], compact[9:]])]
        values.append(compact[:9])
    if len(compact) > 3:
        values.append(compact[:min(4, len(compact) - 1)])
    return list(dict.fromkeys(value for value in values if value))


def matching_offers(offers, row):
    if isinstance(offers, dict):
        offers = offers.get("offers", [])
    return [offer for offer in offers if on_page(row.article, offer.get("product", "")) and on_page(row.brand, offer.get("product", "")) and offer.get("quantity", 0) > 0]


def forum_offers_from_tables(tables):
    """Раскрываем rowspan/colspan: одна ссылка может относиться к нескольким поставкам."""
    offers, seen = [], set()
    for rows in tables:
        grid = [[] for _ in rows]
        for y, row in enumerate(rows):
            x = 0
            for cell in row["cells"]:
                while x < len(grid[y]) and grid[y][x] is not None:
                    x += 1
                width = max(1, min(cell.get("colspan", 1), 30))
                for dy in range(max(1, min(cell.get("rowspan", 1) or len(rows) - y, len(rows) - y))):
                    for dx in range(width):
                        target = grid[y + dy]
                        while len(target) <= x + dx:
                            target.append(None)
                        target[x + dx] = cell
                x += width
        header_index, product_index, quantity_index = None, None, None
        for y, cells in enumerate(grid):
            names = [cell["text"].strip() if cell else "" for cell in cells]
            p = next((i for i, name in enumerate(names) if re.fullmatch("Товар", name, re.I)), None)
            q = next((i for i, name in enumerate(names) if re.search(r"Кол-во\s+для\s+возврата", name, re.I)), None)
            if p is not None and q is not None:
                header_index, product_index, quantity_index = y, p, q
                break
        if header_index is None:
            continue
        for y, cells in enumerate(grid[header_index + 1:], header_index + 1):
            if not rows[y].get("visible", True) or len(cells) <= max(product_index, quantity_index):
                continue
            action = next((cell.get("action") for cell in cells if cell and cell.get("action")), None)
            if not action or not cells[product_index] or not cells[quantity_index]:
                continue
            try:
                quantity = float(cells[quantity_index]["text"].strip().replace(",", "."))
            except ValueError:
                continue
            if not math.isfinite(quantity) or quantity < 0:
                continue
            product = cells[product_index]["text"].strip()
            document = " · ".join(cell["text"] for cell in cells[1:4] if cell)
            signature = (action["href"], document, product)
            if signature in seen:
                continue
            seen.add(signature)
            offers.append({**action, "product": product, "quantity": quantity, "document": document,
                           "document_key": cells[1]["text"] if len(cells) > 1 and cells[1] else ""})
    return offers
