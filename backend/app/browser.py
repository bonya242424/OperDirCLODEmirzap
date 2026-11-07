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
        self.submission_messages: list[str] = []


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

    def _run(self, make, timeout: float = 45.0, retry: bool = True):
        """make: функция без аргументов, возвращающая корутину. При гонке со сменой страницы повторяем один раз."""
        for attempt in ((1, 2) if retry else (1,)):
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
                if transient and attempt == 1 and retry:
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
    RISKY = r"отправ|подтверд|оформить возврат|создать заяв|удал|submit|send|confirm|delete"

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
        text = (await page.evaluate("document.body ? document.body.innerText : ''"))[:limit]
        links = await page.evaluate("""() => Array.from(document.querySelectorAll('a[href]'))
            .filter(a => a.getClientRects().length && a.textContent.trim())
            .slice(0, 60).map(a => ({text: a.textContent.trim().slice(0, 120), href: a.href}))""")
        notices = "\n".join(self._get(key).submission_messages)
        return f"Адрес страницы: {page.url}\n{text}\nСообщения при последней отправке:\n{notices}\nСсылки на странице:\n" + "\n".join(
            f"{link['text']} — {link['href']}" for link in links
        )

    def safe_click(self, key: int, x: float, y: float, allow_return_preview: bool = False) -> None:
        if not (0 <= x <= VIEW_W and 0 <= y <= VIEW_H):
            raise BrowserError("Клик за пределами окна")
        self._run(lambda: self._safe_click(key, x, y, allow_return_preview))

    async def _safe_click(self, key: int, x: float, y: float, allow_return_preview: bool = False) -> None:
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
            is_preview = allow_return_preview and label.strip() == "Оформить возврат" and await page.evaluate(
                "() => location.hostname.endsWith('.armtek.ru') && !/Оформить возврат по фактуре/i.test(document.body.innerText)"
            )
            if not is_preview:
                raise BrowserError(f"Кнопка «{label.strip()}» заблокирована: в режиме проверки ничего не отправляется")
        from playwright.async_api import TimeoutError as PlaywrightTimeout
        from urllib.parse import urlparse

        popup = None
        try:
            async with page.expect_popup(timeout=700) as opened:
                await page.mouse.click(x, y)
            popup = await opened.value
        except PlaywrightTimeout:
            pass
        if popup is not None:
            try:
                await popup.wait_for_load_state("domcontentloaded", timeout=10000)
            except PlaywrightTimeout:
                pass
            if urlparse(popup.url).netloc != urlparse(page.url).netloc:
                raise BrowserError("Новая вкладка ведёт на другой сайт; агент остаётся в кабинете поставщика")
            self._get(key).page = popup

    def controls(self, key: int) -> list[dict]:
        return self._run(lambda: self._controls(key))

    async def _controls(self, key: int) -> list[dict]:
        session = self._get(key)
        session.controls_version = getattr(session, "controls_version", 0) + 1
        return await session.page.evaluate("""version => {
            const elements = Array.from(document.querySelectorAll(
                'input,textarea,select,button,a,[role=option],.ui-menu-item,.select2-results__option,.tt-suggestion,.dropdown-menu li,.autocomplete-suggestion'
            )).filter(e => e.getClientRects().length && !['password','hidden','file'].includes(e.type)).slice(0,150);
            return elements.map((e,index) => {
                const control = version + '-' + index;
                e.setAttribute('data-operdir-control', control);
                const label = (e.labels?.[0]?.innerText || e.placeholder || e.title ||
                    (e.tagName === 'INPUT' ? e.closest('tr')?.cells?.[0]?.innerText : e.innerText) || '').trim().slice(0,180);
                return {control,label,tag:e.tagName,type:e.type || '',name:e.name || '',
                    value:e.value || '',disabled:e.disabled || false,
                    options:e.tagName === 'SELECT' ? Array.from(e.options).map(o => ({value:o.value,text:o.text})) : undefined};
            });
        }""", session.controls_version)

    async def _control(self, key: int, control: str):
        import re
        if not re.fullmatch(r"[0-9]+-[0-9]+", control):
            raise BrowserError("Неверный идентификатор элемента")
        locator = self._get(key).page.locator(f'[data-operdir-control="{control}"]')
        if await locator.count() != 1:
            raise BrowserError("Элемент изменился. Прочитайте browser_controls заново")
        return locator

    def fill_control(self, key: int, control: str, value: str) -> None:
        self._run(lambda: self._fill_control(key, control, value))

    async def _fill_control(self, key: int, control: str, value: str) -> None:
        element = await self._control(key, control)
        info = await element.evaluate("(e) => ({tag:e.tagName,type:e.type,disabled:e.disabled,readonly:e.readOnly})")
        if info["disabled"] or info["readonly"] or info["tag"] not in ("INPUT", "TEXTAREA", "SELECT") or info["type"] in ("password","file","hidden","submit","button","reset","checkbox","radio"):
            raise BrowserError("Это поле нельзя заполнять агенту")
        if info["tag"] == "SELECT":
            await element.select_option(value=value)
        else:
            await element.fill(value)
            await element.press("End")

    def control_point(self, key: int, control: str) -> tuple[float, float]:
        return self._run(lambda: self._control_point(key, control))

    async def _control_point(self, key: int, control: str) -> tuple[float, float]:
        element = await self._control(key, control)
        await element.scroll_into_view_if_needed()
        box = await element.bounding_box()
        if not box:
            raise BrowserError("Элемент не виден")
        return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

    def click_control(self, key: int, control: str, allow_return_preview: bool = False) -> None:
        self._run(lambda: self._click_control(key, control, allow_return_preview))

    async def _click_control(self, key: int, control: str, allow_return_preview: bool) -> None:
        x, y = await self._control_point(key, control)
        await self._safe_click(key, x, y, allow_return_preview)

    def submission_preview(self, key: int, x: float, y: float) -> dict:
        if not (0 <= x <= VIEW_W and 0 <= y <= VIEW_H):
            raise BrowserError("Клик за пределами окна")
        return self._run(lambda: self._submission_preview(key, x, y))

    async def _submission_preview(self, key: int, x: float, y: float) -> dict:
        page = self._get(key).page
        data = await page.evaluate(r"""([x,y]) => {
            const el = document.elementFromPoint(x,y);
            const button = el && el.closest('button,input[type=submit],a,[role=button]');
            if (!button) return {label: '', text: '', items: [], quantities: [], values: ''};
            const root = location.hostname.endsWith('.forum-auto.ru') ? document.body : (button.closest('.modal-content,[role=dialog],.modal') || button.closest('form') || document.body);
            const visible = e => e.getClientRects().length > 0;
            const label = (button.innerText || button.value || '').trim();
            const items = [];
            for (const table of root.querySelectorAll('table')) {
                const headings = Array.from(table.querySelectorAll('tr')).find(r => /Количество\s+к\s+возврату/i.test(r.innerText));
                if (!headings) continue;
                const cells = Array.from(headings.children).map(e => e.innerText.trim());
                const a = cells.findIndex(t => /^(Код|Артикул)$/i.test(t));
                const b = cells.findIndex(t => /^Бренд$/i.test(t));
                const q = cells.findIndex(t => /Количество\s+к\s+возврату/i.test(t));
                if (a < 0 || b < 0 || q < 0) continue;
                for (const row of table.querySelectorAll('tr')) {
                    const c = Array.from(row.children);
                    if (row === headings || !visible(row) || c.length <= Math.max(a,b,q)) continue;
                    const qty = Number(c[q].innerText.trim().replace(',', '.'));
                    if (Number.isFinite(qty)) items.push({article:c[a].innerText.trim(),brand:c[b].innerText.trim(),qty});
                }
            }
            const quantities = [];
            for (const row of root.querySelectorAll('tr')) {
                if (row.querySelector('tr') || !/Кол-во\s+для\s+возврата/i.test(row.innerText)) continue;
                for (const input of row.querySelectorAll('input:not([type=hidden])')) {
                    if (visible(input)) quantities.push(Number(input.value.replace(',', '.')));
                }
            }
            const values = Array.from(root.querySelectorAll('input,textarea,select'))
                .filter(visible).map(e => e.tagName === 'SELECT' ? (e.selectedOptions[0]?.text || '') : e.value).join(' ');
            const text = root.innerText;
            const documentMatch = text.match(/(?:Документ\s*:|фактуре\s*№)\s*([0-9]+)/i);
            return {label,text,items,quantities,values,supplier_document:documentMatch ? documentMatch[1] : ''};
        }""", [x, y])
        return {"url": page.url, "page_text": await page.evaluate("document.body.innerText"), **data}

    def send_return(self, key: int, x: float, y: float, preview: dict) -> None:
        # Не повторяем необратимый клик даже при ошибке навигации.
        self._run(lambda: self._send_return(key, x, y, preview), retry=False)

    def wait_response(self, key: int) -> None:
        self._run(lambda: self._get(key).page.wait_for_timeout(2000))

    async def _send_return(self, key: int, x: float, y: float, preview: dict) -> None:
        if await self._submission_preview(key, x, y) != preview:
            raise BrowserError("Форма изменилась перед отправкой")
        session = self._get(key)
        page = session.page
        session.submission_messages = []

        async def submission_dialog(dialog):
            session.submission_messages.append(f"{dialog.type}: {dialog.message[:1000]}")
            session.submission_messages = session.submission_messages[-10:]
            # Только окно, вызванное единственным подтверждённым кликом отправки.
            # В обычной навигации подтверждения по-прежнему не принимаются.
            if dialog.type in ("confirm", "alert"):
                await dialog.accept()
            else:
                await dialog.dismiss()

        page.on("dialog", submission_dialog)
        try:
            await page.mouse.click(x, y)
            await page.wait_for_timeout(2000)
        finally:
            page.remove_listener("dialog", submission_dialog)

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
        if name in ("Enter", "Space"):
            texts = await page.evaluate(
                """() => { const f = document.activeElement && document.activeElement.form; if (!f) { const el = document.activeElement; return el ? [(el.innerText || el.value || '') + ''] : []; }
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
