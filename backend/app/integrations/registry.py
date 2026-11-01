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
    category: str = "Прочее"
    # Есть ли у источника «выгрузки данных» (для модели ИИ, почты, бота это не имеет смысла).
    has_sync: bool = True


def _d(key, name, description, check, category, has_sync=True) -> IntegrationDef:
    return IntegrationDef(key, name, description, check, category, has_sync)


REGISTRY: list[IntegrationDef] = [
    _d("bitrix24", "Битрикс24", "Задачи выбранных сотрудников и дедлайны", checks.check_bitrix24, "Учёт и CRM"),
    _d(
        "onec_accounting",
        "1С: Бухгалтерия",
        "Выгрузки из бухгалтерии, по ним считаются финансовые показатели",
        checks.check_onec_accounting,
        "Учёт и CRM",
    ),
    _d(
        "onec_trade",
        "1С: Управление торговлей",
        "Выгрузки из УТ (старая версия, отдельная интеграция)",
        checks.check_onec_trade,
        "Учёт и CRM",
    ),
    _d("diadoc", "Диадок", "Электронный документооборот", checks.check_diadoc, "Учёт и CRM"),
    _d(
        "google_sheets_rnp",
        "Google Таблицы: РНП",
        "Три таблицы РНП, ежедневная выгрузка",
        checks.check_google_sheets,
        "Таблицы и документы",
    ),
    _d("google_docs", "Google Документы", "Чтение и создание документов", checks.check_google_docs, "Таблицы и документы"),
    _d("yandex_metrika", "Яндекс Метрика", "Визиты, источники, цели, конверсии", checks.check_yandex_metrika, "Яндекс"),
    _d(
        "yandex_webmaster",
        "Яндекс Вебмастер",
        "Индексация, поисковые запросы, ошибки сайта",
        checks.check_yandex_webmaster,
        "Яндекс",
    ),
    _d("yandex_disk", "Яндекс Диск", "Файлы компании на Диске", checks.check_yandex_disk, "Яндекс", has_sync=False),
    _d("telegram", "Telegram", "Бот: уведомления и общение с системой", checks.check_telegram, "Связь", has_sync=False),
    _d("email_imap", "Почта (IMAP)", "Чтение входящих писем", checks.check_imap, "Связь", has_sync=False),
    _d(
        "google_calendar",
        "Google Календарь",
        "Встречи для общего календаря",
        checks.check_google_calendar,
        "Календари",
        has_sync=False,
    ),
    _d(
        "yandex_calendar",
        "Яндекс Календарь",
        "Встречи и ссылки на Телемост",
        checks.check_yandex_calendar,
        "Календари",
        has_sync=False,
    ),
    _d(
        "ftp",
        "FTP-сервер",
        "Сюда выгружаются файлы для анализа",
        checks.check_ftp,
        "Файлы",
    ),
    _d("mirzap_mcp", "Сайт Mirzap (MCP)", "Данные сайта через MCP-сервер", checks.check_mirzap_mcp, "Сайт", has_sync=False),
    _d("llm", "Модель ИИ", "API модели для чата, агентов и бота", checks.check_llm, "ИИ", has_sync=False),
]

BY_KEY: dict[str, IntegrationDef] = {d.key: d for d in REGISTRY}


def to_row(defn: IntegrationDef) -> Integration:
    return Integration(key=defn.key, name=defn.name, description=defn.description)
