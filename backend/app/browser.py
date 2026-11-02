"""Браузер на сервере для кабинетов поставщиков.

Владелец сам входит в кабинет через окно в интерфейсе (мы пересылаем клики и нажатия клавиш), а мы сохраняем только
cookies сессии в зашифрованном виде. Пароль нигде не сохраняется и не пишется в журнал: нажатия идут прямо в страницу.
Playwright работает в отдельном потоке со своим циклом событий, чтобы не мешать FastAPI.
"""

import asyncio
import logging
import threading
import time
from concurrent.futures import Future
from typing import Any

log = logging.getLogger(__name__)

VIEW_W, VIEW_H = 1100, 700
IDLE_SECONDS = 15 * 60
KEYS = {"Enter", "Tab", "Backspace", "Escape", "ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight", "Delete", "Space"}


class BrowserError(Exception):
    """Текст безопасен для показа пользователю."""


class _Session:
    def __init__(self, context: Any, page: Any) -> None:
        self.context, self.page, self.used = context, page, time.time()


class BrowserManager:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._pw: Any = None
        self._browser: Any = None
        self._sessions: dict[int, _Session] = {}

    # --- поток и цикл событий ---
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                t = threading.Thread(target=loop.run_forever, name="browser-loop", daemon=True)
                t.start()
                self._loop, self._thread = loop, t
            return self._loop

    def _run(self, coro, timeout: float = 45.0):
        fut: Future = asyncio.run_coroutine_threadsafe(coro, self._ensure_loop())
        try:
            return fut.result(timeout=timeout)
        except BrowserError:
            raise
        except TimeoutError:
            fut.cancel()
            raise BrowserError("Браузер не ответил вовремя") from None
        except Exception as exc:  # noqa: BLE001 - наружу только понятный текст
            log.warning("Ошибка браузера: %s", type(exc).__name__)
            raise BrowserError(f"Ошибка браузера ({type(exc).__name__})") from None

    async def _start(self) -> None:
        if self._browser is not None:
            return
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise BrowserError("На сервере не установлен браузер (playwright)") from None
        self._pw = await async_playwright().start()
        try:
            self._browser = await self._pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        except Exception:  # noqa: BLE001
            await self._pw.stop()
            self._pw = None
            raise BrowserError("Не удалось запустить браузер на сервере: проверьте установку chromium") from None

    async def _reap(self) -> None:
        now = time.time()
        for key in [k for k, s in self._sessions.items() if now - s.used > IDLE_SECONDS]:
            await self._close(key)

    def _get(self, key: int) -> _Session:
        s = self._sessions.get(key)
        if s is None:
            raise BrowserError("Окно браузера не открыто: нажмите «Открыть кабинет»")
        s.used = time.time()
        return s

    # --- действия (синхронные обёртки) ---
    def is_open(self, key: int) -> bool:
        return key in self._sessions

    def open(self, key: int, url: str, state: dict | None) -> None:
        self._run(self._open(key, url, state), timeout=60.0)

    async def _open(self, key: int, url: str, state: dict | None) -> None:
        await self._reap()
        await self._start()
        await self._close(key)
        context = await self._browser.new_context(viewport={"width": VIEW_W, "height": VIEW_H}, locale="ru-RU", storage_state=state)
        page = await context.new_page()
        self._sessions[key] = _Session(context, page)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception:  # noqa: BLE001 - окно остаётся открытым, владелец увидит, что со страницей
            log.info("Страница %s не загрузилась до конца", url)

    def screenshot(self, key: int) -> bytes:
        return self._run(self._shot(key))

    async def _shot(self, key: int) -> bytes:
        return await self._get(key).page.screenshot(type="jpeg", quality=70)

    def click(self, key: int, x: float, y: float) -> None:
        if not (0 <= x <= VIEW_W and 0 <= y <= VIEW_H):
            raise BrowserError("Клик за пределами окна")
        self._run(self._click(key, x, y))

    async def _click(self, key: int, x: float, y: float) -> None:
        await self._get(key).page.mouse.click(x, y)

    def type_text(self, key: int, text: str) -> None:
        self._run(self._type(key, text))

    async def _type(self, key: int, text: str) -> None:
        await self._get(key).page.keyboard.type(text, delay=20)

    def press(self, key: int, name: str) -> None:
        if name not in KEYS:
            raise BrowserError("Эта клавиша не поддерживается")
        self._run(self._press(key, "Space" if name == "Space" else name))

    async def _press(self, key: int, name: str) -> None:
        await self._get(key).page.keyboard.press(name)

    def scroll(self, key: int, dy: float) -> None:
        self._run(self._scroll(key, max(-2000.0, min(2000.0, dy))))

    async def _scroll(self, key: int, dy: float) -> None:
        await self._get(key).page.mouse.wheel(0, dy)

    def info(self, key: int) -> dict:
        return self._run(self._info(key))

    async def _info(self, key: int) -> dict:
        page = self._get(key).page
        return {"url": page.url, "title": await page.title()}

    def state(self, key: int) -> dict:
        return self._run(self._state(key))

    async def _state(self, key: int) -> dict:
        return await self._get(key).context.storage_state()

    def close(self, key: int) -> None:
        if key in self._sessions:
            self._run(self._close(key))

    async def _close(self, key: int) -> None:
        s = self._sessions.pop(key, None)
        if s is not None:
            try:
                await s.context.close()
            except Exception:  # noqa: BLE001
                pass


manager = BrowserManager()
