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


def exact_product_index(candidates: list[dict], article: str, brand: str) -> int | None:
    from .return_brands import selected_indices
    matches = selected_indices(candidates, article, brand)
    return matches[0] if len(matches) == 1 else None


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
        if self._browser is not None and self._browser.is_connected():
            return
        if self._browser is not None:
            self._browser = None
            if self._pw is not None:
                await self._pw.stop()
                self._pw = None
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

    def auth_required(self, key: int) -> bool:
        return self._run(lambda: self._auth_required(key))

    async def _auth_required(self, key: int) -> bool:
        return await self._get(key).page.locator('input[type=password]:visible').count() > 0

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



    def session_authenticated(self, key: int) -> bool:
        return self._run(lambda: self._session_authenticated(key))

    async def _session_authenticated(self, key: int) -> bool:
        return await self._evaluate_read(self._get(key).page, """() => {
            const visible=e=>!!e.getClientRects().length;
            if(Array.from(document.querySelectorAll('input[type=password]')).some(visible)) return false;
            if(document.querySelector('a[href*="logout" i],a[href*="logoff" i],a[href*="/exit" i]')) return true;
            if(Array.from(document.querySelectorAll('a,button,[role=button]')).some(e=>/^(Выйти|Выход|Logout|Log out|Sign out)$/i.test(e.innerText.trim()))) return true;
            if(location.hostname.endsWith('.armtek.ru') && location.pathname.includes('/order_return')) return !!document.querySelector('input[name=MATNR]');
            if(location.hostname.endsWith('.forum-auto.ru') && location.pathname.includes('/shop/returns/')) return /Создание возвратов|Поиск документов для создания/.test(document.body.innerText);
            return false;
        }""")

    def login_diagnostic(self, key: int) -> dict:
        return self._run(lambda: self._login_diagnostic(key))

    async def _login_diagnostic(self, key: int) -> dict:
        session = self._get(key)
        data = await self._evaluate_read(session.page, """() => ({
            fields:Array.from(document.querySelectorAll('input')).filter(e=>e.getClientRects().length && e.type!=='hidden').map(e=>({type:e.type,name:e.name,id:e.id,autocomplete:e.autocomplete})).slice(0,20),
            buttons:Array.from(document.querySelectorAll('a,button,input[type=submit],input[type=button],[role=button]')).filter(e=>e.getClientRects().length).map(e=>(e.tagName==='INPUT'?e.value:e.innerText).trim()).filter(t=>/вход|войти|авториз|кабинет|login|sign in|log in/i.test(t) && !t.includes('@')).map(t=>t.slice(0,70)).slice(0,15),
            password_count:Array.from(document.querySelectorAll('input[type=password]')).filter(e=>e.getClientRects().length).length
        })""")
        return {"stage": getattr(session, "login_stage", "open_cabinet"), **data}


    def login(self, key: int, login_url: str, username: str, password: str, target_url: str) -> dict:
        # Не повторять ввод/отправку пароля внутри общего механизма _run.
        return self._run(lambda: self._login(key, login_url, username, password, target_url), timeout=60, retry=False)

    async def _login(self, key: int, login_url: str, username: str, password: str, target_url: str) -> dict:
        import re
        from urllib.parse import urlsplit
        from .supplier_credentials import SUPPLIERS
        page = self._get(key).page
        original_host = (urlsplit(login_url).hostname or "").lower()
        domain = next((d for d in SUPPLIERS if original_host == d or original_host.endswith("." + d)), None)
        if not domain or urlsplit(login_url).scheme != "https":
            raise BrowserError("Недопустимый адрес авторизации")

        def same_site(url):
            parsed = urlsplit(url)
            host = (parsed.hostname or "").lower()
            return parsed.scheme == "https" and (host == domain or host.endswith("." + domain))

        async def authenticated():
            try:
                if await page.locator('input[type="password"]:visible').count():
                    return False
                if await page.locator('a[href*="logout" i],a[href*="logoff" i],a[href*="/exit" i]').count():
                    return True
                if await page.get_by_text(re.compile(r"^(?:Выйти|Выход|Logout|Log out|Sign out)$", re.I)).count():
                    return True
                if domain == "armtek.ru" and "/order_return" in page.url:
                    return await page.locator('input[name="MATNR" i]').count() == 1
                if domain == "forum-auto.ru" and "/shop/returns/" in page.url:
                    return await page.get_by_text(re.compile(r"Создание возвратов|Поиск документов для создания")).count() > 0
                return False
            except Exception as exc:
                if "context was destroyed" in str(exc).lower() or "navigation" in str(exc).lower():
                    return False
                raise

        try:
            self._get(key).login_stage = "open_login"
            await page.goto(login_url, wait_until="domcontentloaded", timeout=25000)
            if not same_site(page.url):
                raise BrowserError("Авторизация перенаправила на другой сайт")
            if await authenticated():
                await page.goto(target_url, wait_until="domcontentloaded", timeout=20000)
                return await self._state(key)
            self._get(key).login_stage = "find_form"
            passwords = page.locator('input[type="password"]:visible')
            if await passwords.count() == 0:
                # SPA может дорисовать кнопку входа уже после DOMContentLoaded.
                entry = page.locator('a:visible,button:visible,[role=button]:visible').filter(has_text=re.compile(r"^\s*(?:Войти|Вход|Авторизация|Личный кабинет|Кабинет клиента|Личный кабинет клиента|Войти на сайт|Вход на сайт|Войти в личный кабинет|Войти в кабинет|Вход в кабинет|Вход в ЛК|Войти в ЛК|Вход / Регистрация|Вход для клиентов|Вход для партнеров|Login|Sign in)\s*$", re.I))
                for _ in range(30):
                    if await passwords.count():
                        break
                    count = await entry.count()
                    if count == 1:
                        await entry.click()
                        break
                    if count > 1:
                        targets = await entry.evaluate_all("els=>els.map(e=>e.getAttribute('href') || '')")
                        if targets and all(targets) and len(set(targets)) == 1:
                            await entry.first.click()
                            break
                    await page.wait_for_timeout(500)
            await passwords.first.wait_for(state="visible", timeout=20000)
            if await passwords.count() != 1 or not same_site(page.url):
                raise BrowserError("Не найдена однозначная форма входа")
            password_field = passwords.first
            root = password_field.locator("xpath=ancestor::form[1]")
            if await root.count() != 1:
                root = page.locator("body")
            form_target = await root.get_attribute("action") or page.url
            from urllib.parse import urljoin
            if not same_site(urljoin(page.url, form_target)):
                raise BrowserError("Форма входа отправляет данные на другой сайт")
            self._get(key).login_stage = "find_username"
            users = root.locator(
                'input[autocomplete="username"]:visible,input[type="email"]:visible,input[name*="login" i]:visible,input[name*="user" i]:visible,input[id*="login" i]:visible,input[name*="email" i]:visible,input[id*="email" i]:visible')
            # Исключаем пароль даже если его имя содержит user/login.
            users = users.locator('xpath=self::input[not(@type="password") and not(@type="hidden") and not(@type="submit") and not(@type="button")]')
            if await users.count() != 1:
                users = root.locator('input[type="text"]:visible,input[type="email"]:visible,input:not([type]):visible')
            if await users.count() != 1:
                raise BrowserError("Не найдено однозначное поле логина")
            self._get(key).login_stage = "find_submit"
            submit = root.locator('button:visible,input[type="submit"]:visible,input[type="button"]:visible,[role="button"]:visible').filter(
                has_text=re.compile(r"^\s*(?:Войти|Вход|Вход в систему|Войти на сайт|Вход на сайт|Войти в кабинет|Войти в личный кабинет|Войти в ЛК|Вход в ЛК|Авторизоваться|Авторизация|Login|Sign in|Log in)\s*$", re.I))
            if await submit.count() != 1:
                submit = root.locator('button[type="submit"]:visible,input[type="submit"]:visible')
            if await submit.count() != 1:
                raise BrowserError("Не найдена однозначная кнопка входа")
            label = await submit.get_attribute("value") or await submit.inner_text()
            if not re.fullmatch(r"\s*(?:Войти|Вход|Вход в систему|Войти на сайт|Вход на сайт|Войти в кабинет|Войти в личный кабинет|Войти в ЛК|Вход в ЛК|Авторизоваться|Авторизация|Login|Sign in|Log in|Продолжить)\s*", label, re.I):
                raise BrowserError("Кнопка формы не является входом")
            self._get(key).login_stage = "fill_credentials"
            await users.fill(username)
            await password_field.fill(password)
            # Последняя проверка адреса непосредственно перед отправкой секретов.
            if not same_site(page.url):
                raise BrowserError("Страница авторизации изменилась")
            self._get(key).login_stage = "submit_login"
            await submit.click()
            self._get(key).login_stage = "verify_login"
            for _ in range(20):
                await page.wait_for_timeout(500)
                if not same_site(page.url):
                    raise BrowserError("Вход перенаправил на другой сайт")
                if await authenticated():
                    await page.goto(target_url, wait_until="domcontentloaded", timeout=20000)
                    if not same_site(page.url) or await page.locator('input[type="password"]:visible').count():
                        raise BrowserError("Сессия кабинета не подтверждена")
                    return await self._state(key)
            raise BrowserError("Сайт не подтвердил авторизацию")
        except BrowserError:
            raise
        except Exception:
            # Playwright может включить аргументы fill в traceback: наружу только фиксированный текст.
            raise BrowserError("Автоматический вход не выполнен: форма изменилась или сайт не ответил") from None

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
    RISKY = r"отправ|подтверд|оформить возврат|создать заяв|удал|submit|send|confirm|delete|\bвернуть\b"

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

    async def _evaluate_read(self, page, expression, *args):
        """Retry only DOM inspection after navigation, never a click or submission."""
        for attempt in range(3):
            try:
                return await page.evaluate(expression, *args)
            except Exception as exc:
                message = str(exc).lower()
                if "execution context was destroyed" not in message and "cannot find context with specified id" not in message:
                    raise
                if attempt == 2:
                    raise
                await page.wait_for_load_state("domcontentloaded", timeout=15000)
                await page.wait_for_timeout(300)

    async def _read(self, key: int, limit: int) -> str:
        page = self._get(key).page
        await page.wait_for_load_state("domcontentloaded")
        text = (await self._evaluate_read(page, "document.body ? document.body.innerText : ''"))[:limit]
        links = await self._evaluate_read(page, """() => Array.from(document.querySelectorAll('a[href]'))
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
            const dismiss=el.closest('[data-dismiss=modal],[data-bs-dismiss=modal],.modal .close,[role=dialog] .close,.btn-close');
            if(dismiss && !dismiss.matches('input[type=submit],button[type=submit]')) {
                const text=(dismiss.innerText || dismiss.getAttribute('aria-label') || dismiss.title || '').trim();
                if(/^(?:[×✕x]|закрыть|close)?$/i.test(text)) return 'Закрыть окно';
            }
            const t = el.closest('button,a,input,[role=button],label,li,td'); const n = t || el;
            return ((n.innerText || n.value || n.title || n.getAttribute('aria-label') || '') + '').slice(0, 120); }""",
            [x, y],
        )
        if re.search(self.RISKY, label, re.I):
            is_preview = allow_return_preview and label.strip() == "Оформить возврат" and await page.evaluate(
                "() => location.hostname.endsWith('.armtek.ru') && !/Оформить возврат по фактуре/i.test(document.body.innerText)"
            )
            if allow_return_preview and not is_preview:
                from urllib.parse import urlsplit
                host = (urlsplit(page.url).hostname or "").lower()
                is_preview = (host == "motexc.ru" or host.endswith(".motexc.ru")) and label.strip() == "Отправить заявку на возврат"
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
        return await self._evaluate_read(session.page, """version => {
            const elements = Array.from(document.querySelectorAll(
                'input,textarea,select,button,a,[role=option],.ui-menu-item,.select2-results__option,.tt-suggestion,#MATNR .autocomplete-container #list-search > li,.dropdown-menu li,.autocomplete-suggestion,[data-operdir-suggestion],[data-dismiss=modal],[data-bs-dismiss=modal],.modal .close,[role=dialog] .close,.btn-close'
            )).filter(e => e.getClientRects().length && !['password','hidden','file'].includes(e.type) && !e.matches('#MATNR .autocomplete-container a.brand-text')).slice(0,150);
            return elements.map((e,index) => {
                const previous = e.getAttribute('data-operdir-control');
                const control = previous && /^[0-9]+-[0-9]+$/.test(previous) && document.querySelectorAll('[data-operdir-control="' + previous + '"]').length === 1 ? previous : version + '-' + index;
                e.setAttribute('data-operdir-control', control);
                const label = (e.labels?.[0]?.innerText || e.placeholder || e.title ||
                    (e.tagName === 'INPUT' ? e.closest('tr')?.cells?.[0]?.innerText : e.innerText) || e.getAttribute('aria-label') || e.name || e.id || '').trim().slice(0,180);
                return {control,label,tag:e.tagName,type:e.type || '',name:e.name || '',
                    id:e.id || '',value:e.value || '',disabled:e.disabled || false,
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

    def search_article(self, key: int, control: str, value: str, *, article: str = "", brand: str = "") -> dict:
        return self._run(lambda: self._search_article(key, control, value, article=article, brand=brand))

    def armtek_search_page(self, key: int, url: str) -> dict:
        return self._run(lambda: self._armtek_search_page(key, url))

    async def _armtek_search_page(self, key: int, url: str) -> dict:
        from urllib.parse import urlsplit
        session = self._get(key)
        target = urlsplit(url)
        for page in session.context.pages:
            current = urlsplit(page.url)
            if current.scheme == target.scheme and current.netloc == target.netloc and current.path.rstrip('/') == target.path.rstrip('/'):
                session.page = page
                await page.bring_to_front()
                break
        else:
            await session.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        return {"page": await self._read(key, 6000), "controls": await self._controls(key)}

    async def _search_article(self, key: int, control: str, value: str, *, article: str = "", brand: str = "") -> dict:
        import re

        page = self._get(key).page
        if article and brand:
            state = await self._evaluate_read(page, """() => ({selected:!!document.querySelector('#MATNR > div .glyphicon-remove'), dates:['date-with','date-to'].map(id=>({id,value:document.getElementById(id)?.value || ''}))})""")
            if state.get("selected"):
                from urllib.parse import urlsplit, urlunsplit
                address = urlsplit(page.url)
                await page.goto(urlunsplit((address.scheme, address.netloc, "/order_return", "", "")), wait_until="domcontentloaded", timeout=30000)
                for field in state["dates"]:
                    if field["value"]:
                        date_field = page.locator('[id="' + field["id"] + '"]')
                        await date_field.fill(field["value"])
                        await date_field.press("Tab")
                control = ""

        async def valid(element):
            info = await element.evaluate("e => ({tag:e.tagName,type:e.type,name:e.name || '',id:e.id || '',disabled:e.disabled,label:[...Array.from(e.labels || []).map(l=>l.innerText), e.placeholder, e.title, e.getAttribute('aria-label'), e.closest('tr')?.cells?.[0]?.innerText].filter(Boolean).join(' ')})")
            return (info["tag"] == "INPUT" and info["type"] in ("text", "search", "")
                    and not info.get("disabled") and (any(str(info.get(k, "")).upper() == "MATNR" for k in ("name", "id"))
                    or re.search(r"артикул|код.*материал", info.get("label", ""), re.I)))

        element = None
        if control:
            try:
                candidate = await self._control(key, control)
                if await valid(candidate):
                    element = candidate
            except BrowserError:
                pass  # старый ID не нужен, когда поле однозначно определяется по MATNR
        if element is None:
            candidate = self._get(key).page.locator('input[name="MATNR" i]:visible, input[id="MATNR" i]:visible')
            if await candidate.count() != 1 or not await valid(candidate):
                raise BrowserError("Не найдено единственное доступное поле артикула MATNR. Прочитайте browser_controls заново")
            element = candidate
        page = self._get(key).page
        from .return_lookup import LookupTrace
        trace = LookupTrace(page, value)
        trace.start()
        try:
            # Виджеты могут перерисовать/сократить ввод: не считаем такой запрос выполненным.
            for delay in (35, 100):
                await element.fill("")
                await element.press_sequentially(value, delay=delay)
                await page.wait_for_timeout(250)
                actual = await element.input_value()
                if actual == value:
                    break
            else:
                raise BrowserError(f"Артикул введён не полностью: ожидалось «{value}», в поле «{actual}». Поиск не подтверждён")
            suggestions = []
            waited = 0
            for _ in range(32):
                suggestions = await self._article_suggestions(key, value)
                if suggestions:
                    break
                await page.wait_for_timeout(250)
                waited += 250
            actual = await element.input_value()
            if actual != value:
                raise BrowserError(f"Поле изменилось при ожидании подсказок: ожидалось «{value}», получено «{actual}»")
            field = await element.evaluate("e => ({name:e.name || '', id:e.id || '', max_length:e.maxLength, aria_controls:e.getAttribute('aria-controls') || ''})")
            network = await trace.snapshot()
            selected = {}
            if article and brand and suggestions:
                candidates = await self._evaluate_read(page, """() => Array.from(document.querySelectorAll('#MATNR .autocomplete-container #list-search > li')).filter(e=>e.getClientRects().length).map(e=>({article:e.querySelector('span')?.innerText || '',brand:e.querySelector('a.brand-text')?.innerText || ''}))""")
                index = exact_product_index(candidates, article, brand)
                if index is not None:
                    items = page.locator('#MATNR .autocomplete-container #list-search > li:visible')
                    # The brand anchor opens information, the article selects the product.
                    await items.nth(index).locator('span').first.click()
                    chosen = await self._evaluate_read(page, """() => Array.from(document.querySelectorAll('#MATNR > div')).filter(e=>e.querySelector('i.glyphicon-remove')).map(e=>({article:e.querySelector('span')?.innerText || '',brand:e.querySelector('a.brand-text')?.innerText || ''}))""")
                    if len(chosen) != 1 or exact_product_index(chosen, article, brand) != 0:
                        raise BrowserError("Выбор артикула и бренда не подтверждён: перечитайте страницу")
                    selected = {"selected": True, "selected_article": chosen[0]["article"], "selected_brand": chosen[0]["brand"]}
            controls = await self._controls(key)
            return {**selected, "lookup_requests": trace.requests_seen, "lookup_responses": network, "field": field, "requested_value": value, "input_value": actual, "input_verified": True,
                    "suggestions": suggestions, "waited_ms": waited, "controls": controls}
        finally:
            trace.close()

    async def _article_suggestions(self, key: int, value: str) -> list[str]:
        return await self._evaluate_read(self._get(key).page, r"""query => {
            const field=document.activeElement;
            const visible=e=>!!e.getClientRects().length && getComputedStyle(e).visibility!=='hidden';
            const norm=s=>(s || '').toUpperCase().replace(/[\s./-]+/g,'');
            const candidates=new Set(document.querySelectorAll('[role=option],.ui-menu-item,.select2-results__option,.tt-suggestion,#MATNR .autocomplete-container #list-search > li,.dropdown-menu li,.autocomplete-suggestion,.ac_results li,.easy-autocomplete-container li,.autocomplete-items > div,.list-group-item,.k-list-item,.k-item,.ui-autocomplete li'));
            for(const id of ((field?.getAttribute('aria-controls') || '')+' '+(field?.getAttribute('aria-owns') || '')).split(/\s+/).filter(Boolean)) {
                const list=document.getElementById(id);
                if(list) {
                    const items=list.querySelectorAll('[role=option],li,tr');
                    for(const item of items.length ? items : list.children) candidates.add(item);
                }
            }
            // Связанный список может показывать только бренд: не отбрасываем его из-за отсутствия артикула в тексте.
            const rows=Array.from(candidates).filter(e=>visible(e) && e.innerText.trim());
            const leaves=rows.filter(e=>!rows.some(other=>other!==e && e.contains(other)));
            return leaves.slice(0,30).map(e=>{
                e.setAttribute('data-operdir-suggestion','true');
                return e.innerText.trim();
            }).filter(Boolean);
        }""", value)

    def forum_returns(self, key: int) -> dict:
        return self._run(lambda: self._forum_returns(key))

    def find_product(self, key: int, article: str, brand: str) -> dict:
        return self._run(lambda: self._find_product(key, article, brand))

    async def _find_product(self, key: int, article: str, brand: str) -> dict:
        session = self._get(key)
        session.controls_version = getattr(session, "controls_version", 0) + 1
        rows = await self._evaluate_read(session.page, r"""({article,brand,version}) => {
            const norm=s=>s.toUpperCase().replace(/[\s./-]+/g,'');
            let index=0;
            return Array.from(document.querySelectorAll('tr')).filter(row=>!row.querySelector('tr') && row.getClientRects().length && norm(row.innerText).includes(norm(article))).map(row=>({
                text:row.innerText,
                brand:(() => {
                    const headers=Array.from(row.closest('table')?.rows[0]?.cells || []).map(c=>c.innerText.trim());
                    const index=headers.findIndex(s=>/^(?:Бренд|Производитель|Марка|Brand|Manufacturer)$/i.test(s));
                    const fallback=location.hostname.endsWith('.armtek.ru') ? 2 : -1;
                    return (row.getAttribute('data-brand') || Array.from(row.cells)[index>=0 ? index : fallback]?.innerText || '').trim();
                })(),
                headers:Array.from(row.closest('table')?.rows[0]?.cells || []).map(c=>c.innerText.trim()),
                controls:Array.from(row.querySelectorAll('input,select,button,a')).filter(e=>e.getClientRects().length && !['hidden','password','file'].includes(e.type)).map(e=>{
                    const previous=e.getAttribute('data-operdir-control');
                    const control=previous && /^[0-9]+-[0-9]+$/.test(previous) && document.querySelectorAll('[data-operdir-control="'+previous+'"]').length===1 ? previous : version+'-'+index++;e.setAttribute('data-operdir-control',control);
                    return {control,tag:e.tagName,type:e.type || '',label:(e.labels?.[0]?.innerText || e.placeholder || e.title || e.innerText || e.name || '').trim(),value:e.value || '',href:e.href || '',disabled:!!e.disabled,
                        options:e.tagName==='SELECT'?Array.from(e.options).map(o=>({value:o.value,text:o.text})):undefined};
                })
            }));
        }""", {"article": article, "brand": brand, "version": session.controls_version})
        from .return_verification import on_page
        from .return_brands import selected_indices
        article_rows = [row for row in rows if on_page(article, row["text"])]
        if article_rows and all(row.get("brand") for row in article_rows):
            indices = selected_indices([{"article": article, "brand": row["brand"]} for row in article_rows], article, brand)
            matched = [article_rows[index] for index in indices]
        else:
            matched = [row for row in article_rows if on_page(brand, row["text"])]
        result = {"article": article, "brand": brand, "rows": matched}
        if matched and matched[0].get("brand"):
            result["selected_brand"] = matched[0]["brand"]
        if not matched:
            result["diagnostics"] = await self._evaluate_read(session.page, r"""({article,brand}) => {
                const norm=s=>(s || '').toUpperCase().replace(/[\s./-]+/g,'');
                const rows=Array.from(document.querySelectorAll('tr')).filter(r=>r.getClientRects().length);
                const text=r=>r.innerText+' '+Array.from(r.querySelectorAll('[alt],[title],[aria-label]')).map(e=>e.alt || e.title || e.getAttribute('aria-label')).join(' ');
                const articleRows=rows.filter(r=>norm(text(r)).includes(norm(article)));
                return {article_rows:articleRows.length,brand_rows:rows.filter(r=>norm(text(r)).includes(norm(brand))).length,
                    candidates:articleRows.slice(-4).map(r=>({text:text(r).slice(0,600),nested_rows:r.querySelectorAll('tr').length}))};
            }""", {"article": article, "brand": brand})
        return result

    async def _forum_returns(self, key: int) -> dict:
        await self._controls(key)
        raw = await self._evaluate_read(self._get(key).page, r"""() => ({
            tables:Array.from(document.querySelectorAll('table')).map(table=>Array.from(table.rows).map(row=>({
                visible:!!row.getClientRects().length,
                cells:Array.from(row.cells).map(cell=>{
                    const link=Array.from(cell.querySelectorAll('a')).find(a=>/Отказ\s+от\s+товара/i.test(a.innerText) && !a.hasAttribute('disabled') && a.getAttribute('aria-disabled')!=='true' && a.href);
                    return {text:cell.innerText.trim(),rowspan:cell.rowSpan,colspan:cell.colSpan,
                        action:link?{href:link.href,control:link.getAttribute('data-operdir-control')}:null};
                })
            }))),
            has_creation_section:/Создание\s+возвратов/i.test(document.body.innerText),
            search_values:Array.from(document.querySelectorAll('input')).filter(e=>e.getClientRects().length && ['text','search',''].includes(e.type)).map(e=>e.value).filter(Boolean)
        })""")
        from .return_search import forum_offers_from_tables
        return {"offers": forum_offers_from_tables(raw.pop("tables")), **raw}

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

            const guided = !/(?:^|\.)armtek\.ru$|(?:^|\.)forum-auto\.ru$/.test(location.hostname);
            const unmapped = [];
            if (guided) {
                items.length = 0;
                const mapped = new Set();
                for (const table of root.querySelectorAll('table')) {
                    const heading = Array.from(table.rows).find(r=>Array.from(r.cells).some(c=>/^(?:Артикул|Код|Номер детали|Номер запчасти|Article|SKU|Part number)$/i.test(c.innerText.trim())));
                    if (!heading) continue;
                    const labels = Array.from(heading.cells).map(c=>c.innerText.trim());
                    const a=labels.findIndex(s=>/^(?:Артикул|Код|Номер детали|Номер запчасти|Article|SKU|Part number)$/i.test(s));
                    const b=labels.findIndex(s=>/^(?:Бренд|Производитель|Марка|Brand|Manufacturer)$/i.test(s));
                    let q=labels.findIndex(s=>/к возврату|возвращаем|количеств[оа] возврата/i.test(s));
                    if(q<0) q=labels.findIndex(s=>/^(?:Кол[.-]?во|Количество|Quantity|Qty)(?:\s|$)/i.test(s));
                    if(a<0 || q<0) continue;
                    for(const row of Array.from(table.rows)) {
                        if(row===heading || !visible(row) || row.cells.length<=Math.max(a,q,b)) continue;
                        const checks=Array.from(row.querySelectorAll('input[type=checkbox]'));
                        if(checks.length && !checks.some(c=>c.checked)) continue;
                        const cell=row.cells[q];
                        const fields=Array.from(cell.querySelectorAll('input')).filter(e=>!['password','checkbox','radio'].includes(e.type));
                        fields.forEach(e=>mapped.add(e));
                        const nums=fields.map(e=>Number(e.value.replace(',','.'))).filter(n=>Number.isFinite(n));
                        const distinct=[...new Set(nums)];
                        if(distinct.length>1) {unmapped.push('conflicting quantity fields');continue;}
                        const qty=nums.length?nums[0]:Number(cell.innerText.trim().replace(',','.'));
                        if(!Number.isFinite(qty) || qty<=0) continue;
                        items.push({article:row.cells[a].innerText.trim(),brand:b>=0?row.cells[b].innerText.trim():'',
                                    qty,text:row.innerText});
                    }
                }
                for(const field of root.querySelectorAll('input:enabled')) {
                    const label=[field.name,field.id,field.placeholder,...Array.from(field.labels || []).map(l=>l.innerText)].join(' ');
                    if(visible(field) && /quantity|qty|количеств|кол-во/i.test(label) && !mapped.has(field) && Number(field.value)>0)
                        unmapped.push('unmapped quantity field');
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
            return {label,text,items,quantities,values,supplier_document:documentMatch ? documentMatch[1] : '',
                root_scope:root.matches('.modal-content,[role=dialog],.modal')?'dialog':root.tagName==='FORM'?'form':'body',
                form_valid:root.tagName==='FORM'?root.checkValidity():!Array.from(root.querySelectorAll('input,select,textarea')).some(e=>e.willValidate&&!e.checkValidity()),
                unmapped_quantities:unmapped};
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
