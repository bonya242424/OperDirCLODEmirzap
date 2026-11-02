"""Список прокси для запросов к ИИ. Пароли хранятся зашифрованными и наружу не отдаются."""

from urllib.parse import quote

import httpx
from sqlalchemy import select

from .config import settings
from .connections import decrypt, encrypt
from .db import SessionLocal
from .models import Proxy, utcnow


def build_url(p: Proxy) -> str:
    auth = ""
    if p.username:
        password = decrypt(p.password_encrypted).get("password", "") if p.password_encrypted else ""
        auth = f"{quote(p.username, safe='')}:{quote(password, safe='')}@"
    return f"{p.scheme}://{auth}{p.host}:{p.port}"


def seal_password(password: str) -> str:
    return encrypt({"password": password}) if password else ""


def active_proxies() -> list[tuple[int, str]]:
    with SessionLocal() as db:
        rows = db.scalars(select(Proxy).where(Proxy.enabled.is_(True)).order_by(Proxy.priority, Proxy.id))
        return [(p.id, build_url(p)) for p in rows]


def record_result(proxy_id: int, ok: bool) -> None:
    with SessionLocal() as db:
        p = db.get(Proxy, proxy_id)
        if p is None:
            return
        p.last_status, p.last_check_at = ("ok" if ok else "error"), utcnow()
        p.last_message = "" if ok else "Запрос через прокси не прошёл"
        db.commit()


def check_proxy(p: Proxy) -> tuple[bool, str, int | None]:
    """Ходит через прокси к адресу API модели. Любой HTTP-ответ значит, что прокси работает."""
    target = (settings.llm_base_url.strip() or "https://api.anthropic.com").rstrip("/")
    import time

    started = time.monotonic()
    try:
        with httpx.Client(proxy=build_url(p), timeout=15.0) as client:
            client.get(target)
    except httpx.ProxyError:
        return False, "Прокси отклонил соединение: проверьте логин, пароль и тип", None
    except httpx.TimeoutException:
        return False, "Прокси не ответил за 15 секунд", None
    except httpx.HTTPError as exc:
        return False, f"Не удалось соединиться ({type(exc).__name__})", None
    except Exception:  # noqa: BLE001
        return False, "Неверные настройки прокси", None
    return True, "Работает: API модели достижим", int((time.monotonic() - started) * 1000)
