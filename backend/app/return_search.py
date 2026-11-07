"""Варианты артикула для поиска без подмены товара и бренда."""

import re

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
