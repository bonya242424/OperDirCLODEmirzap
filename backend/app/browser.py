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
KEYS = {"Enter", "Tab", "Backspace", "Escape", "ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight", "Delete", "Space", "Home", "End", "PageUp", "PageDown", "Insert", "ContextMenu", "CapsLock"}
MODS = {"Control", "Shift", "Alt", "Meta"}


def valid_key(name: str) -> bool:
    """Клавиша или сочетание: Enter, Control+a, Shift+Tab, F5. Лишнего не пропускаем."""
    parts = name.split("+")
    if not parts or len(set(parts)) != len(parts) or not set(parts[:-1]) <= MODS:
        return False
    last = parts[-1]
    return last in KEYS or (len(last) == 1 and last.isprintable()) or (last.startswith("F") and last[1:].isdigit() and 1 <= int(last[1:]) <= 12)


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

    def _run(self, make, timeout: float = 45.0):
        """make: функция без аргументов, возвращающая корутину. При гонке со сменой страницы повторяем один раз."""
        for attempt in (1, 2):
            fut: Future = asyncio.run_coroutine_threadsafe(make(), self._ensure_loop())
            try:
                return fut.result(timeout=timeout)
            except BrowserError:
                raise
            except TimeoutError:
                fut.cancel()
                raise BrowserError("Браузер не ответил вовремя") from None
            except Exception as exc:  # noqa: BLE001 - наружу только понятный текст
                msg = (str(exc).strip().splitlines() or [""])[0][:200]
                transient = "context was destroyed" in msg or "navigat" in msg.lower()
                if transient and attempt == 1:
                    time.sleep(1.0)
                    continue
                log.warning("Ошибка браузера: %s: %s", type(exc).__name__, msg)
                raise BrowserError(f"Ошибка браузера: {msg or type(exc).__name__}") from None

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
        self._run(lambda: self._open(key, url, state), timeout=60.0)

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
        return self._run(lambda: self._shot(key))

    async def _shot(self, key: int) -> bytes:
        return await self._get(key).page.screenshot(type="jpeg", quality=70)

    def click(self, key: int, x: float, y: float, button: str = "left", double: bool = False) -> None:
        if not (0 <= x <= VIEW_W and 0 <= y <= VIEW_H):
            raise BrowserError("Клик за пределами окна")
        if button not in ("left", "right", "middle"):
            raise BrowserError("Неизвестная кнопка мыши")
        self._run(lambda: self._click(key, x, y, button, double))

    async def _click(self, key: int, x: float, y: float, button: str = "left", double: bool = False) -> None:
        await self._get(key).page.mouse.click(x, y, button=button, click_count=2 if double else 1)

    def type_text(self, key: int, text: str) -> None:
        self._run(lambda: self._type(key, text))

    async def _type(self, key: int, text: str) -> None:
        await self._get(key).page.keyboard.type(text, delay=20)

    def press(self, key: int, name: str) -> None:
        if not valid_key(name):
            raise BrowserError("Эта клавиша не поддерживается")
        self._run(lambda: self._press(key, name))

    async def _press(self, key: int, name: str) -> None:
        await self._get(key).page.keyboard.press(name)

    def scroll(self, key: int, dy: float) -> None:
        self._run(lambda: self._scroll(key, max(-2000.0, min(2000.0, dy))))

    async def _scroll(self, key: int, dy: float) -> None:
        await self._get(key).page.mouse.wheel(0, dy)

    # --- свободная навигация владельца (не для агента) ---
    def navigate(self, key: int, url: str) -> None:
        self._run(lambda: self._navigate(key, url), timeout=60.0)

    async def _navigate(self, key: int, url: str) -> None:
        page = self._get(key).page
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:  # noqa: BLE001
            msg = (str(exc).strip().splitlines() or [""])[0][:200]
            raise BrowserError(f"Страница не открылась: {msg}") from None

    def history(self, key: int, action: str) -> None:
        if action not in ("back", "forward", "reload"):
            raise BrowserError("Неизвестное действие")
        self._run(lambda: self._history(key, action), timeout=60.0)

    async def _history(self, key: int, action: str) -> None:
        page = self._get(key).page
        try:
            await {"back": page.go_back, "forward": page.go_forward, "reload": page.reload}[action](wait_until="domcontentloaded", timeout=30000)
        except Exception:  # noqa: BLE001 - нет истории или страница не успела
            pass

    # --- режим проверки для агента: опасные действия блокируются кодом, а не просьбой к модели ---
    RISKY = r"отправ|подтверд|создать заяв|удал|submit|send|confirm|delete"

    def goto(self, key: int, url: str) -> None:
        self._run(lambda: self._goto(key, url), timeout=60.0)

    async def _goto(self, key: int, url: str) -> None:
        page = self._get(key).page
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:  # noqa: BLE001 - редирект или прерванный переход: страница могла загрузиться другая
            msg = (str(exc).strip().splitlines() or [""])[0][:200]
            if page.url in ("", "about:blank"):
                raise BrowserError(f"Страница не открылась: {msg or type(exc).__name__}") from None
            log.info("Переход на %s закончился иначе: %s", url, msg)

    def read_text(self, key: int, limit: int = 6000) -> str:
        return self._run(lambda: self._read(key, limit))

    async def _read(self, key: int, limit: int) -> str:
        page = self._get(key).page
        await page.wait_for_load_state("domcontentloaded")
        return (await page.evaluate("document.body ? document.body.innerText : ''"))[:limit]

    def safe_click(self, key: int, x: float, y: float) -> None:
        if not (0 <= x <= VIEW_W and 0 <= y <= VIEW_H):
            raise BrowserError("Клик за пределами окна")
        self._run(lambda: self._safe_click(key, x, y))

    async def _safe_click(self, key: int, x: float, y: float) -> None:
        import re

        page = self._get(key).page
        await page.wait_for_load_state("domcontentloaded")
        label = await page.evaluate(
            """([x, y]) => { const el = document.elementFromPoint(x, y); if (!el) return '';
            const t = el.closest('button,a,input,[role=button],label,li,td'); const n = t || el;
            return ((n.innerText || n.value || n.title || n.getAttribute('aria-label') || '') + '').slice(0, 120); }""",
            [x, y],
        )
        if re.search(self.RISKY, label, re.I):
            raise BrowserError(f"Кнопка «{label.strip()}» заблокирована: в режиме проверки ничего не отправляется")
        await page.mouse.click(x, y)

    def safe_type(self, key: int, text: str, clear: bool = True) -> None:
        self._run(lambda: self._safe_type(key, text, clear))

    async def _safe_type(self, key: int, text: str, clear: bool = True) -> None:
        page = self._get(key).page
        kind = await page.evaluate("(document.activeElement && document.activeElement.type) || ''")
        if kind == "password":
            raise BrowserError("Пароли агент не вводит: войдите сами через «Войти в кабинет»")
        if clear:
            await page.keyboard.press("Control+A")  # заменяем содержимое поля, а не дописываем к нему
        await page.keyboard.type(text, delay=20)

    def safe_press(self, key: int, name: str) -> None:
        if name not in KEYS:
            raise BrowserError("Эта клавиша не поддерживается")
        self._run(lambda: self._safe_press(key, name))

    async def _safe_press(self, key: int, name: str) -> None:
        import re

        page = self._get(key).page
        if name == "Enter":
            texts = await page.evaluate(
                """() => { const f = document.activeElement && document.activeElement.form; if (!f) return [];
                return [...f.querySelectorAll('button,input[type=submit]')].map(b => (b.innerText || b.value || '') + ''); }"""
            )
            if any(re.search(self.RISKY, t, re.I) for t in texts):
                raise BrowserError("Enter заблокирован: в этой форме есть кнопка отправки")
        await page.keyboard.press(name)

    def info(self, key: int) -> dict:
        return self._run(lambda: self._info(key))

    async def _info(self, key: int) -> dict:
        page = self._get(key).page
        return {"url": page.url, "title": await page.title()}

    def state(self, key: int) -> dict:
        return self._run(lambda: self._state(key))

    async def _state(self, key: int) -> dict:
        return await self._get(key).context.storage_state()

    def close(self, key: int) -> None:
        if key in self._sessions:
            self._run(lambda: self._close(key))

    async def _close(self, key: int) -> None:
        s = self._sessions.pop(key, None)
        if s is not None:
            try:
                await s.context.close()
            except Exception:  # noqa: BLE001
                pass


manager = BrowserManager()
