"""Подключения пользователей и компании: описание типов, шифрование, проверки.

Правила те же, что у общих интеграций: в сообщения не попадают токены, пароли и тексты исключений.
"""

import base64
import hashlib
import json
from dataclasses import dataclass, field

import httpx
from cryptography.fernet import Fernet, InvalidToken

from .config import settings
from .integrations.checks import ERROR, NOT_CONFIGURED, OK, CheckResult, _get


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    secret: bool = False
    required: bool = True
    default: str = ""


@dataclass(frozen=True)
class Kind:
    key: str
    name: str
    description: str
    fields: list[Field] = field(default_factory=list)


KINDS: dict[str, Kind] = {
    k.key: k
    for k in [
        Kind("telegram", "Telegram", "Бот для уведомлений и общения", [Field("bot_token", "Токен бота", secret=True)]),
        Kind(
            "email_imap",
            "Почта (IMAP)",
            "Чтение входящих писем",
            [
                Field("host", "Сервер IMAP"),
                Field("port", "Порт", default="993"),
                Field("user", "Логин"),
                Field("password", "Пароль", secret=True),
            ],
        ),
        Kind(
            "google_sheet",
            "Google Таблица",
            "Ссылка на таблицу с открытым доступом",
            [Field("spreadsheet_id", "Ссылка или id таблицы")],
        ),
        Kind(
            "anthropic",
            "Ключ Claude",
            "Ключ API модели для чата, агентов и разбора видео. Только для компании, один на всех",
            [Field("api_key", "Ключ API (sk-ant-…)", secret=True)],
        ),
        Kind(
            "openai",
            "Ключ GPT (OpenAI)",
            "Ключ API OpenAI. Включается переключателем «Модель ИИ» в «Интеграции». Только для компании, один на всех",
            [Field("api_key", "Ключ API (sk-…)", secret=True)],
        ),
        Kind(
            "yandex_metrika",
            "Яндекс Метрика",
            "Визиты, источники, цели, конверсии. Один счётчик на подключение",
            [Field("oauth_token", "OAuth-токен", secret=True), Field("counter_id", "Номер счётчика")],
        ),
        Kind("yandex_webmaster", "Яндекс Вебмастер", "Индексация и ошибки сайта", [Field("oauth_token", "OAuth-токен", secret=True)]),
        Kind(
            "rdp",
            "Терминал (RDP)",
            "Удалённый рабочий стол, например терминал 1С. Открывается в разделе «Удалённые окна»",
            [
                Field("host", "Адрес сервера"),
                Field("port", "Порт", default="3389"),
                Field("user", "Логин"),
                Field("password", "Пароль", secret=True),
                Field("domain", "Домен", required=False),
            ],
        ),
        Kind("yandex_disk", "Яндекс Диск", "Файлы на Диске", [Field("oauth_token", "OAuth-токен", secret=True)]),
        Kind(
            "ftp",
            "FTP-сервер",
            "Папка с файлами",
            [
                Field("host", "Сервер"),
                Field("port", "Порт", default="21"),
                Field("user", "Логин"),
                Field("password", "Пароль", secret=True),
                Field("dir", "Папка", required=False, default="/"),
                Field("tls", "FTPS (да/нет)", required=False, default="нет"),
            ],
        ),
    ]
}


def _fernet() -> Fernet:
    digest = hashlib.sha256(("connections:" + settings.secret_key).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(config: dict[str, str]) -> str:
    return _fernet().encrypt(json.dumps(config, ensure_ascii=False).encode()).decode()


def decrypt(value: str) -> dict[str, str]:
    try:
        return json.loads(_fernet().decrypt(value.encode())) if value else {}
    except (InvalidToken, ValueError):
        return {}


def spreadsheet_id(value: str) -> str:
    value = value.strip()
    if "/d/" in value:
        value = value.split("/d/", 1)[1].split("/", 1)[0]
    return value


def check_connection(kind: str, cfg: dict[str, str]) -> CheckResult:
    spec = KINDS[kind]
    if any(f.required and not cfg.get(f.name, "").strip() for f in spec.fields):
        return CheckResult(NOT_CONFIGURED, "Заполнены не все поля")
    try:
        return _CHECKS[kind](cfg)
    except Exception as exc:  # noqa: BLE001 - наружу уходит только тип ошибки
        return CheckResult(ERROR, f"Проверка не удалась ({type(exc).__name__})")


def _telegram(cfg):
    resp, err = _get(f"https://api.telegram.org/bot{cfg['bot_token'].strip()}/getMe")
    if err:
        return CheckResult(ERROR, err)
    if resp.status_code in (401, 404):
        return CheckResult(ERROR, "Telegram не принял токен бота")
    if resp.status_code != 200:
        return CheckResult(ERROR, f"Telegram ответил кодом {resp.status_code}")
    return CheckResult(OK, "Бот отвечает")


def _imap(cfg):
    import imaplib

    conn = None
    try:
        conn = imaplib.IMAP4_SSL(cfg["host"].strip(), int(cfg.get("port") or 993), timeout=10)
        conn.login(cfg["user"], cfg["password"])
    except imaplib.IMAP4.error:
        return CheckResult(ERROR, "Почтовый сервер не принял логин или пароль")
    except (OSError, EOFError, ValueError) as exc:
        return CheckResult(ERROR, f"Нет соединения с почтовым сервером ({type(exc).__name__})")
    finally:
        if conn is not None:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass
    return CheckResult(OK, "Вход в почтовый ящик выполняется")


def _sheet(cfg):
    from .integrations.checks import check_public_sheet

    return check_public_sheet(spreadsheet_id(cfg["spreadsheet_id"]))


def _disk(cfg):
    resp, err = _get("https://cloud-api.yandex.net/v1/disk", {"Authorization": f"OAuth {cfg['oauth_token'].strip()}"})
    if err:
        return CheckResult(ERROR, err)
    if resp.status_code in (401, 403):
        return CheckResult(ERROR, "Токен недействителен или нет прав на Диск")
    if resp.status_code != 200:
        return CheckResult(ERROR, f"Яндекс Диск ответил кодом {resp.status_code}")
    return CheckResult(OK, "Диск доступен")


def _ftp(cfg):
    import ftplib

    tls = cfg.get("tls", "").strip().lower() in ("да", "yes", "true", "1")
    ftp = None
    try:
        ftp = ftplib.FTP_TLS(timeout=10) if tls else ftplib.FTP(timeout=10)
        ftp.connect(cfg["host"].strip(), int(cfg.get("port") or 21))
        ftp.login(cfg["user"], cfg["password"])
        if tls:
            ftp.prot_p()
        ftp.cwd(cfg.get("dir") or "/")
    except ftplib.error_perm:
        return CheckResult(ERROR, "FTP-сервер не принял логин, пароль или папку")
    except (ftplib.Error, OSError, EOFError, ValueError) as exc:
        return CheckResult(ERROR, f"Нет соединения с FTP-сервером ({type(exc).__name__})")
    finally:
        if ftp is not None:
            try:
                ftp.quit()
            except Exception:  # noqa: BLE001
                pass
    return CheckResult(OK, "FTP-сервер доступен, папка открывается")


def _yandex_check(url: str, cfg: dict[str, str], what: str) -> CheckResult:
    resp, err = _get(url, {"Authorization": f"OAuth {cfg['oauth_token'].strip()}"})
    if err:
        return CheckResult(ERROR, err)
    if resp.status_code in (401, 403):
        return CheckResult(ERROR, f"Токен недействителен или нет доступа: {what}")
    if resp.status_code == 404:
        return CheckResult(ERROR, f"{what}: не найдено, проверьте номер")
    if resp.status_code != 200:
        return CheckResult(ERROR, f"{what} ответил кодом {resp.status_code}")
    return CheckResult(OK, f"{what}: подключение работает")


def _metrika(cfg):
    counter = cfg["counter_id"].strip()
    if not counter.isdigit():
        return CheckResult(ERROR, "Номер счётчика состоит только из цифр")
    return _yandex_check(f"https://api-metrika.yandex.net/management/v1/counter/{counter}", cfg, "Метрика")


def _webmaster(cfg):
    return _yandex_check("https://api.webmaster.yandex.net/v4/user", cfg, "Вебмастер")


def _rdp(cfg):
    import socket

    try:
        port = int(cfg.get("port") or 3389)
    except ValueError:
        return CheckResult(ERROR, "Порт должен быть числом")
    try:
        with socket.create_connection((cfg["host"].strip(), port), timeout=6):
            pass
    except OSError as exc:
        return CheckResult(ERROR, f"Сервер не отвечает на порту {port} ({type(exc).__name__}): проверьте адрес, порт и доступ с нашего сервера")
    return CheckResult(OK, f"Сервер отвечает на порту {port}. Логин и пароль проверяются при подключении")


def _anthropic(cfg):
    from .integrations.checks import check_anthropic_key

    key = cfg["api_key"].strip()
    if not key.startswith("sk-ant-"):
        return CheckResult(ERROR, "Ключ должен начинаться с sk-ant-")
    return check_anthropic_key(key)


def _openai(cfg):
    from .integrations.checks import check_openai_key

    key = cfg["api_key"].strip()
    if not key.startswith("sk-"):
        return CheckResult(ERROR, "Ключ должен начинаться с sk-")
    return check_openai_key(key)


_CHECKS = {"openai": _openai, "anthropic": _anthropic, "rdp": _rdp, "yandex_metrika": _metrika, "yandex_webmaster": _webmaster, "telegram": _telegram, "email_imap": _imap, "google_sheet": _sheet, "yandex_disk": _disk, "ftp": _ftp}
