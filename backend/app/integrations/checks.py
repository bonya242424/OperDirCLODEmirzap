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


def check_public_sheet(sheet_id: str) -> CheckResult:
    """Таблица открыта по ссылке: пробуем скачать её как CSV без входа в аккаунт."""
    resp, err = _get(f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv")
    if err:
        return CheckResult(ERROR, err)
    assert resp is not None
    if resp.status_code in (301, 302, 303, 307, 308, 401, 403, 404):
        return CheckResult(ERROR, "Таблица закрыта или ссылка неверная: откройте доступ по ссылке")
    if resp.status_code != 200:
        return CheckResult(ERROR, f"Google ответил кодом {resp.status_code}")
    if "text/html" in resp.headers.get("content-type", ""):
        return CheckResult(ERROR, "Вместо таблицы пришла страница входа: откройте доступ по ссылке")
    return CheckResult(OK, "Таблица доступна по ссылке")


def check_google_sheets(db, row: Integration) -> CheckResult:
    ids = [i.strip() for i in settings.rnp_spreadsheet_ids.split(",") if i.strip()]
    if not ids:
        return CheckResult(NOT_CONFIGURED, "Не задан RNP_SPREADSHEET_IDS (id или ссылки на таблицы через запятую)")
    problems: list[str] = []
    for number, raw in enumerate(ids, 1):
        sheet_id = raw.split("/d/", 1)[1].split("/", 1)[0] if "/d/" in raw else raw
        result = check_public_sheet(sheet_id)
        if result.status != OK:
            problems.append(f"таблица №{number}: {result.message}")
    if problems:
        return CheckResult(ERROR, "; ".join(problems))
    return freshness(row, 26, "Таблицы доступны, данные свежие")


def _onec(row: Integration, what: str) -> CheckResult:
    if row.last_sync_at is None:
        return CheckResult(NOT_CONFIGURED, f"Выгрузка из 1С ({what}) ещё не загружалась")
    return freshness(row, settings.onec_expected_interval_hours, "Файл выгрузки свежий")


def check_onec_accounting(db, row: Integration) -> CheckResult:
    return _onec(row, "Бухгалтерия")


def check_onec_trade(db, row: Integration) -> CheckResult:
    return _onec(row, "Управление торговлей")


def _pending(configured: bool, missing: str) -> CheckResult:
    """Для интеграций, которые ещё не написаны: честно говорим, что проверки пока нет."""
    if not configured:
        return CheckResult(NOT_CONFIGURED, missing)
    return CheckResult(WARNING, "Данные заданы, но автоматическая проверка появится вместе с интеграцией")


def check_telegram(db, row: Integration) -> CheckResult:
    token = settings.telegram_bot_token.strip()
    if not token:
        return CheckResult(NOT_CONFIGURED, "Не задан TELEGRAM_BOT_TOKEN")
    resp, err = _get(f"https://api.telegram.org/bot{token}/getMe")
    if err:
        return CheckResult(ERROR, err)
    assert resp is not None
    if resp.status_code in (401, 404):
        return CheckResult(ERROR, "Telegram не принял токен бота")
    if resp.status_code != 200:
        return CheckResult(ERROR, f"Telegram ответил кодом {resp.status_code}")
    return CheckResult(OK, "Бот отвечает")


def check_imap(db, row: Integration) -> CheckResult:
    import imaplib

    if not (settings.imap_host.strip() and settings.imap_user.strip() and settings.imap_password):
        return CheckResult(NOT_CONFIGURED, "Не заданы IMAP_HOST, IMAP_USER и IMAP_PASSWORD")
    conn = None
    try:
        cls = imaplib.IMAP4_SSL if settings.imap_ssl else imaplib.IMAP4
        conn = cls(settings.imap_host.strip(), settings.imap_port, timeout=10)
        conn.login(settings.imap_user, settings.imap_password)
    except imaplib.IMAP4.error:
        return CheckResult(ERROR, "Почтовый сервер не принял логин или пароль")
    except (OSError, EOFError) as exc:
        return CheckResult(ERROR, f"Нет соединения с почтовым сервером ({type(exc).__name__})")
    finally:
        if conn is not None:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001 - соединение уже могло закрыться
                pass
    return CheckResult(OK, "Вход в почтовый ящик выполняется")


def check_yandex_disk(db, row: Integration) -> CheckResult:
    if not settings.yandex_oauth_token.strip():
        return CheckResult(NOT_CONFIGURED, "Не задан YANDEX_OAUTH_TOKEN")
    resp, err = _get("https://cloud-api.yandex.net/v1/disk", _yandex_headers())
    if err:
        return CheckResult(ERROR, err)
    assert resp is not None
    if resp.status_code in (401, 403):
        return CheckResult(ERROR, "Токен недействителен или у него нет прав на Диск")
    if resp.status_code != 200:
        return CheckResult(ERROR, f"Яндекс Диск ответил кодом {resp.status_code}")
    return CheckResult(OK, "Диск доступен")


def check_ftp(db, row: Integration) -> CheckResult:
    import ftplib

    if not (settings.ftp_host.strip() and settings.ftp_user.strip() and settings.ftp_password):
        return CheckResult(NOT_CONFIGURED, "Не заданы FTP_HOST, FTP_USER и FTP_PASSWORD")
    ftp = None
    try:
        ftp = ftplib.FTP_TLS(timeout=10) if settings.ftp_tls else ftplib.FTP(timeout=10)
        ftp.connect(settings.ftp_host.strip(), settings.ftp_port)
        ftp.login(settings.ftp_user, settings.ftp_password)
        if settings.ftp_tls:
            ftp.prot_p()
        ftp.cwd(settings.ftp_dir or "/")
    except ftplib.error_perm:
        return CheckResult(ERROR, "FTP-сервер не принял логин, пароль или папку")
    except (ftplib.Error, OSError, EOFError) as exc:
        return CheckResult(ERROR, f"Нет соединения с FTP-сервером ({type(exc).__name__})")
    finally:
        if ftp is not None:
            try:
                ftp.quit()
            except Exception:  # noqa: BLE001
                pass
    return CheckResult(OK, "FTP-сервер доступен, папка открывается")


def check_diadoc(db, row: Integration) -> CheckResult:
    s = settings
    return _pending(bool(s.diadoc_client_id and s.diadoc_login and s.diadoc_password), "Не заданы DIADOC_CLIENT_ID, DIADOC_LOGIN, DIADOC_PASSWORD")


def check_google_docs(db, row: Integration) -> CheckResult:
    return _pending(bool(settings.google_service_account_file.strip()), "Не задан GOOGLE_SERVICE_ACCOUNT_FILE")


def check_google_calendar(db, row: Integration) -> CheckResult:
    return _pending(
        bool(settings.google_service_account_file.strip() and settings.google_calendar_ids.strip()),
        "Не заданы GOOGLE_SERVICE_ACCOUNT_FILE и GOOGLE_CALENDAR_IDS",
    )


def check_yandex_calendar(db, row: Integration) -> CheckResult:
    return _pending(
        bool(settings.yandex_calendar_login and settings.yandex_calendar_password),
        "Не заданы YANDEX_CALENDAR_LOGIN и YANDEX_CALENDAR_PASSWORD",
    )


def check_mirzap_mcp(db, row: Integration) -> CheckResult:
    return _pending(bool(settings.mirzap_mcp_url.strip()), "Не задан MIRZAP_MCP_URL")


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
