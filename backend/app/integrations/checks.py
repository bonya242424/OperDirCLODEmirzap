"""Проверки состояния интеграций.

Каждая проверка получает строку Integration и возвращает CheckResult.
Правила, общие для всех проверок:
  * в сообщения не попадают URL, токены и тексты исключений: вебхук Битрикс24
    содержит секрет прямо в адресе;
  * «ok» означает, что подключение работает и данные свежие; если подключение
    работает, но данные давно не выгружались, статус «warning».
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from ..config import settings
from ..models import Integration

log = logging.getLogger(__name__)

OK = "ok"
WARNING = "warning"
ERROR = "error"
NOT_CONFIGURED = "not_configured"


@dataclass
class CheckResult:
    status: str
    message: str


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def freshness(row: Integration, max_age_hours: int, ok_message: str) -> CheckResult:
    """Подключение исправно; оцениваем, насколько свежие данные."""
    if row.last_sync_at is None:
        return CheckResult(OK, "Подключение работает, данные ещё не выгружались")
    age = datetime.now(timezone.utc) - _aware(row.last_sync_at)
    if age > timedelta(hours=max_age_hours):
        hours = int(age.total_seconds() // 3600)
        return CheckResult(
            WARNING,
            f"Данные устарели: последняя выгрузка {hours} ч назад (норма: не реже раза в {max_age_hours} ч)",
        )
    return CheckResult(OK, ok_message)


def _get(url: str, headers: dict[str, str] | None = None) -> tuple[httpx.Response | None, str | None]:
    """GET без утечки адреса в текст ошибки."""
    try:
        return httpx.get(url, headers=headers, timeout=10.0), None
    except httpx.TimeoutException:
        return None, "Сервис не ответил за 10 секунд"
    except httpx.HTTPError as exc:
        return None, f"Ошибка сети ({type(exc).__name__})"


def check_bitrix24(db, row: Integration) -> CheckResult:
    webhook = settings.bitrix24_webhook_url.strip()
    if not webhook:
        return CheckResult(NOT_CONFIGURED, "Не задан BITRIX24_WEBHOOK_URL")
    resp, err = _get(webhook.rstrip("/") + "/profile.json")
    if err:
        return CheckResult(ERROR, err)
    assert resp is not None
    if resp.status_code != 200:
        return CheckResult(ERROR, f"Битрикс24 ответил кодом {resp.status_code}: проверьте вебхук и его права")
    try:
        data = resp.json()
    except ValueError:
        return CheckResult(ERROR, "Ответ не похож на ответ Битрикс24: проверьте адрес вебхука")
    if not isinstance(data, dict) or "result" not in data:
        return CheckResult(ERROR, "Битрикс24 вернул ошибку: проверьте вебхук и его права")
    return freshness(row, 2, "Подключение работает, данные свежие")


def check_google_sheets(db, row: Integration) -> CheckResult:
    key_file = settings.google_service_account_file.strip()
    ids = [i.strip() for i in settings.rnp_spreadsheet_ids.split(",") if i.strip()]
    if not key_file or not ids:
        return CheckResult(NOT_CONFIGURED, "Не заданы GOOGLE_SERVICE_ACCOUNT_FILE и RNP_SPREADSHEET_IDS")
    try:
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_file(
            key_file, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
        )
        session = AuthorizedSession(creds)
    except (OSError, ValueError) as exc:
        return CheckResult(ERROR, f"Не удалось прочитать файл ключа Google ({type(exc).__name__})")

    problems: list[str] = []
    for number, sheet_id in enumerate(ids, 1):
        try:
            resp = session.get(
                f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}",
                params={"fields": "properties.title"},
                timeout=10,
            )
        except Exception as exc:  # noqa: BLE001 - библиотеки google/requests бросают разные типы
            problems.append(f"таблица №{number}: ошибка сети ({type(exc).__name__})")
            continue
        if resp.status_code != 200:
            problems.append(f"таблица №{number}: код {resp.status_code}")
    if problems:
        return CheckResult(ERROR, "Нет доступа: " + "; ".join(problems))
    return freshness(row, 26, "Таблицы доступны, данные свежие")


def check_onec_files(db, row: Integration) -> CheckResult:
    hours = settings.onec_expected_interval_hours
    if row.last_sync_at is None:
        return CheckResult(NOT_CONFIGURED, "Файлы выгрузки 1С ещё не загружались")
    return freshness(row, hours, "Файл выгрузки свежий")


def _yandex_headers() -> dict[str, str]:
    return {"Authorization": f"OAuth {settings.yandex_oauth_token.strip()}"}


def check_yandex_metrika(db, row: Integration) -> CheckResult:
    token = settings.yandex_oauth_token.strip()
    counter = settings.yandex_metrika_counter_id.strip()
    if not token or not counter:
        return CheckResult(NOT_CONFIGURED, "Не заданы YANDEX_OAUTH_TOKEN и YANDEX_METRIKA_COUNTER_ID")
    resp, err = _get(f"https://api-metrika.yandex.net/management/v1/counter/{counter}", _yandex_headers())
    if err:
        return CheckResult(ERROR, err)
    assert resp is not None
    if resp.status_code in (401, 403):
        return CheckResult(ERROR, "Токен недействителен или нет доступа к счётчику")
    if resp.status_code != 200:
        return CheckResult(ERROR, f"Метрика ответила кодом {resp.status_code}")
    return freshness(row, 26, "Подключение работает, данные свежие")


def check_yandex_webmaster(db, row: Integration) -> CheckResult:
    token = settings.yandex_oauth_token.strip()
    if not token:
        return CheckResult(NOT_CONFIGURED, "Не задан YANDEX_OAUTH_TOKEN")
    resp, err = _get("https://api.webmaster.yandex.net/v4/user", _yandex_headers())
    if err:
        return CheckResult(ERROR, err)
    assert resp is not None
    if resp.status_code in (401, 403):
        return CheckResult(ERROR, "Токен недействителен или нет доступа к Вебмастеру")
    if resp.status_code != 200:
        return CheckResult(ERROR, f"Вебмастер ответил кодом {resp.status_code}")
    return freshness(row, 26, "Подключение работает, данные свежие")


def check_llm(db, row: Integration) -> CheckResult:
    if not settings.anthropic_api_key.strip():
        return CheckResult(NOT_CONFIGURED, "Не задан ANTHROPIC_API_KEY")
    try:
        from anthropic import Anthropic, APIConnectionError, APIStatusError

        client = Anthropic(
            api_key=settings.anthropic_api_key,
            base_url=settings.llm_base_url or None,
            timeout=10.0,
            max_retries=0,
        )
        client.models.list(limit=1)
    except APIStatusError as exc:
        return CheckResult(ERROR, f"API модели ответил кодом {exc.status_code}")
    except APIConnectionError:
        return CheckResult(ERROR, "Нет соединения с API модели")
    return CheckResult(OK, "API модели отвечает")
