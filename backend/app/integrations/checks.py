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
    token = _yandex_token("yandex_disk")
    if not token:
        return CheckResult(NOT_CONFIGURED, "Не задан токен: добавьте подключение «Яндекс Диск» или YANDEX_OAUTH_TOKEN")
    resp, err = _get("https://cloud-api.yandex.net/v1/disk", _yandex_headers(token))
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


def company_cfg(kind: str) -> dict[str, str]:
    """Настройки из подключения компании (раздел «Интеграции → Подключения»); пусто, если его нет."""
    from sqlalchemy import select

    from ..connections import decrypt
    from ..db import SessionLocal
    from ..models import Connection

    with SessionLocal() as db:
        row = db.scalar(select(Connection).where(Connection.kind == kind, Connection.owner_id.is_(None)).order_by(Connection.id.desc()))
        return decrypt(row.config_encrypted) if row else {}


def _yandex_token(kind: str) -> str:
    """Токен Яндекса: из подключения компании данного типа, иначе из YANDEX_OAUTH_TOKEN."""
    return company_cfg(kind).get("oauth_token", "").strip() or settings.yandex_oauth_token.strip()


def _yandex_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"OAuth {token}"}


def check_yandex_metrika(db, row: Integration) -> CheckResult:
    cfg = company_cfg("yandex_metrika")
    token = _yandex_token("yandex_metrika")
    counter = cfg.get("counter_id", "").strip() or settings.yandex_metrika_counter_id.strip()
    if not token or not counter:
        return CheckResult(NOT_CONFIGURED, "Не заданы токен и номер счётчика: добавьте подключение «Яндекс Метрика»")
    if not counter.isdigit():
        return CheckResult(ERROR, "Номер счётчика состоит только из цифр")
    resp, err = _get(f"https://api-metrika.yandex.net/management/v1/counter/{counter}", _yandex_headers(token))
    if err:
        return CheckResult(ERROR, err)
    assert resp is not None
    if resp.status_code in (401, 403):
        return CheckResult(ERROR, "Токен недействителен или нет доступа к счётчику")
    if resp.status_code != 200:
        return CheckResult(ERROR, f"Метрика ответила кодом {resp.status_code}")
    return freshness(row, 26, "Подключение работает, данные свежие")


def check_yandex_webmaster(db, row: Integration) -> CheckResult:
    token = _yandex_token("yandex_webmaster")
    if not token:
        return CheckResult(NOT_CONFIGURED, "Не задан токен: добавьте подключение «Яндекс Вебмастер»")
    resp, err = _get("https://api.webmaster.yandex.net/v4/user", _yandex_headers(token))
    if err:
        return CheckResult(ERROR, err)
    assert resp is not None
    if resp.status_code in (401, 403):
        return CheckResult(ERROR, "Токен недействителен или нет доступа к Вебмастеру")
    if resp.status_code != 200:
        return CheckResult(ERROR, f"Вебмастер ответил кодом {resp.status_code}")
    return freshness(row, 26, "Подключение работает, данные свежие")


def check_llm(db, row: Integration) -> CheckResult:
    from ..llm import active_model, active_provider, llm_api_key, llm_base_url, openai_api_key, xai_api_key

    if active_provider() == "xai":
        key = xai_api_key()
        if not key:
            return CheckResult(NOT_CONFIGURED, "Выбран Grok, но ключ не задан: добавьте «Ключ Grok» в «Интеграции»")
        res = check_openai_key(key, llm_base_url("xai"), "Grok")
    elif active_provider() == "openai":
        key = openai_api_key()
        if not key:
            return CheckResult(NOT_CONFIGURED, "Выбран GPT, но ключ не задан: добавьте «Ключ GPT» в «Интеграции»")
        res = check_openai_key(key, llm_base_url("openai"))
    else:
        key = llm_api_key()
        if not key:
            return CheckResult(NOT_CONFIGURED, "Не задан ключ модели: добавьте его в «Интеграции» или в ANTHROPIC_API_KEY")
        base = llm_base_url("anthropic")
        res = check_anthropic_key(key, base)
        if res.status == OK and not base:
            res = check_anthropic_model(key, active_model("anthropic"), res)
    if res.status == OK:
        res = _probe_tools(res)
    if res.status == OK:
        res = CheckResult(OK, f"{res.message}. Модель: {active_model()}, инструменты работают")
    return res


_PROBE: dict[tuple, tuple[float, CheckResult]] = {}


def _probe_tools(ok: CheckResult) -> CheckResult:
    """Агентам и чату нужны вызовы инструментов. Некоторые посредники их не передают: проверяем заранее, раз в 10 минут на выбранную модель."""
    import time

    from ..llm import LLMError, active_model, active_provider, get_provider, llm_base_url

    kind = active_provider()
    sig = (kind, active_model(kind), llm_base_url(kind))
    hit = _PROBE.get(sig)
    if hit and time.monotonic() - hit[0] < 600:
        return hit[1]
    provider = get_provider()
    if provider is None:
        return ok
    tools = [{"name": "ping", "description": "Вызови, чтобы подтвердить связь.", "input_schema": {"type": "object", "properties": {}}}]
    try:
        step = provider.step("Ты проверяешь связь. Единственное, что нужно сделать: вызвать инструмент ping.", [{"role": "user", "content": "Вызови ping."}], tools)
    except LLMError as exc:
        res = CheckResult(ERROR, f"Ключ принят, но запрос к модели «{sig[1]}» не прошёл: {exc}")
    else:
        if step.calls:
            res = ok
        else:
            res = CheckResult(ERROR, f"Ключ принят, но модель «{sig[1]}» не вызывает инструменты (возможно, посредник их не передаёт). Агенты и чат с данными так работать не будут: попробуйте другой адрес или модель")
    _PROBE[sig] = (time.monotonic(), res)
    return res


def check_anthropic_model(key: str, model: str, ok: CheckResult) -> CheckResult:
    """Выбранная версия должна быть доступна именно этому ключу: проверяем её отдельно."""
    from anthropic import Anthropic, APIConnectionError, APIStatusError, DefaultHttpxClient

    from ..proxies import active_proxies

    routes = active_proxies() or [(None, "")]
    for proxy_id, url in routes:
        client = Anthropic(api_key=key, base_url=settings.llm_base_url or None, timeout=10.0, max_retries=0, http_client=DefaultHttpxClient(proxy=url, timeout=10.0) if url else None)
        try:
            client.models.retrieve(model)
        except APIStatusError as exc:
            reason = {404: "такой версии нет", 403: "версия не открыта для вашего ключа"}.get(exc.status_code, f"код {exc.status_code}")
            return CheckResult(ERROR, f"Версия «{model}» недоступна: {reason}. Выберите другую в «Интеграции → Модель ИИ»")
        except APIConnectionError:
            continue
        return ok
    return ok


def check_openai_key(key: str, base: str = "", name: str = "GPT") -> CheckResult:
    from ..proxies import active_proxies, record_result

    routes: list[tuple[int | None, str]] = active_proxies() or [(None, "")]
    for proxy_id, url in routes:
        try:
            resp = httpx.get((base or settings.openai_base_url).rstrip("/") + "/models", headers={"Authorization": f"Bearer {key}"}, timeout=10.0, proxy=url or None)
        except httpx.TransportError:
            if proxy_id is not None:
                record_result(proxy_id, False)
            continue
        if proxy_id is not None:
            record_result(proxy_id, True)
        if resp.status_code in (401, 403):
            return CheckResult(ERROR, f"Ключ {name} недействителен или нет доступа")
        if resp.status_code != 200:
            try:
                j = resp.json()
                err = j.get("error", j) if isinstance(j, dict) else j
                detail = str(err.get("message", err) if isinstance(err, dict) else err)
            except Exception:  # noqa: BLE001
                detail = resp.text
            detail = " ".join(detail.split())[:200]
            return CheckResult(ERROR, f"API модели ответил кодом {resp.status_code}" + (f": {detail}" if detail else ""))
        return CheckResult(OK, f"API {name} отвечает" + (" через прокси" if proxy_id is not None else ""))
    return CheckResult(ERROR, "Нет соединения с API модели" + (": ни один прокси не отвечает" if routes[0][0] is not None else ""))


def check_anthropic_key(key: str, base: str = "") -> CheckResult:
    from anthropic import Anthropic, APIConnectionError, APIStatusError, DefaultHttpxClient

    from ..proxies import active_proxies, record_result

    # Если прокси заданы, прямого доступа нет (сервер в РФ): проверяем через них по очереди.
    routes: list[tuple[int | None, str]] = active_proxies() or [(None, "")]
    for proxy_id, url in routes:
        client = Anthropic(
            api_key=key,
            base_url=base or settings.llm_base_url or None,
            timeout=10.0,
            max_retries=0,
            http_client=DefaultHttpxClient(proxy=url, timeout=10.0) if url else None,
        )
        try:
            client.models.list(limit=1)
        except APIStatusError as exc:
            return CheckResult(ERROR, f"API модели ответил кодом {exc.status_code}")
        except APIConnectionError:
            if proxy_id is not None:
                record_result(proxy_id, False)
            continue
        if proxy_id is not None:
            record_result(proxy_id, True)
        return CheckResult(OK, "API модели отвечает" + (" через прокси" if proxy_id is not None else ""))
    return CheckResult(ERROR, "Нет соединения с API модели" + (": ни один прокси не отвечает" if routes[0][0] is not None else ""))
