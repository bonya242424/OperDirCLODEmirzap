from collections.abc import Callable
from dataclasses import dataclass

from ..models import Integration
from . import checks


@dataclass(frozen=True)
class IntegrationDef:
    key: str
    name: str
    description: str
    check: Callable[..., checks.CheckResult]
    # Есть ли у источника «выгрузки данных» (для модели ИИ это не имеет смысла).
    has_sync: bool = True


REGISTRY: list[IntegrationDef] = [
    IntegrationDef(
        "google_sheets_rnp",
        "Google Sheets: РНП",
        "Три таблицы РНП, ежедневная выгрузка",
        checks.check_google_sheets,
    ),
    IntegrationDef(
        "bitrix24",
        "Битрикс24",
        "Задачи выбранных сотрудников и дедлайны",
        checks.check_bitrix24,
    ),
    IntegrationDef(
        "onec_files",
        "1С: файлы выгрузки",
        "Файлы из раздела «Файлы», по которым считаются показатели",
        checks.check_onec_files,
    ),
    IntegrationDef(
        "yandex_metrika",
        "Яндекс Метрика",
        "Визиты, источники, цели, конверсии",
        checks.check_yandex_metrika,
    ),
    IntegrationDef(
        "yandex_webmaster",
        "Яндекс Вебмастер",
        "Индексация, поисковые запросы, ошибки сайта",
        checks.check_yandex_webmaster,
    ),
    IntegrationDef(
        "llm",
        "Модель ИИ",
        "API модели для чата, агентов и бота",
        checks.check_llm,
        has_sync=False,
    ),
]

BY_KEY: dict[str, IntegrationDef] = {d.key: d for d in REGISTRY}


def to_row(defn: IntegrationDef) -> Integration:
    return Integration(key=defn.key, name=defn.name, description=defn.description)
