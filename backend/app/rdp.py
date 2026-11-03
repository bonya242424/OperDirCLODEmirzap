"""Удалённый рабочий стол (RDP) на сервере: Xvfb + xfreerdp, картинка и ввод через xdotool.

Пароль берётся из зашифрованного подключения компании, наружу не отдаётся и не пишется в журнал.
Команды с паролем в ошибки и логи не попадают.
"""

import logging
import re
import os
import shutil
import subprocess
import time
from pathlib import Path

from .browser import BrowserError, valid_key

log = logging.getLogger(__name__)

W, H = 1280, 800
IDLE_SECONDS = 30 * 60
FIRST_DISPLAY = 100

XKEYS = {
    "Enter": "Return", "Backspace": "BackSpace", "Escape": "Escape", "Tab": "Tab", "Delete": "Delete", "Space": "space",
    "ArrowDown": "Down", "ArrowUp": "Up", "ArrowLeft": "Left", "ArrowRight": "Right",
    "Home": "Home", "End": "End", "PageUp": "Prior", "PageDown": "Next", "Insert": "Insert", "ContextMenu": "Menu", "CapsLock": "Caps_Lock",
}
XMODS = {"Control": "ctrl", "Shift": "shift", "Alt": "alt", "Meta": "super"}


def to_xdotool(name: str) -> str:
    parts = name.split("+")
    keys = [XMODS[p] for p in parts[:-1]]
    last = parts[-1]
    keys.append(XKEYS.get(last, last))
    return "+".join(keys)


class _Session:
    def __init__(self, display: int, xvfb: "subprocess.Popen", rdp: "subprocess.Popen") -> None:
        self.display, self.xvfb, self.rdp, self.used = display, xvfb, rdp, time.time()


class RdpManager:
    def __init__(self) -> None:
        self._sessions: dict[int, _Session] = {}
        # подменяются в тестах
        self.spawn = lambda args, env=None: subprocess.Popen(args, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.spawn_logged = self._spawn_logged
        self.precheck = self._precheck
        self.progress: dict[int, dict] = {}
        self.last_log: dict[int, str] = {}
        self._pending: dict[int, int] = {}
        self.run = lambda args, env=None, timeout=10: subprocess.run(args, env=env, capture_output=True, timeout=timeout, check=True).stdout
        self.wait_display = self._wait_display

    @staticmethod
    def _spawn_logged(args, env, logpath):
        with open(logpath, "wb") as f:
            return subprocess.Popen(args, env=env, stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT)

    @staticmethod
    def tail(log: str, password: str, lines: int = 3) -> str:
        """Последние строки журнала клиента без пароля и логина, чтобы показать настоящий код ошибки."""
        text = log.replace(password, "***") if password else log
        rows = [" ".join(r.split())[:140] for r in text.splitlines() if r.strip()]
        return " | ".join(rows[-lines:])[:400]

    @staticmethod
    def code(log: str) -> str:
        """Короткий код ошибки клиента из журнала: ERRCONNECT_… или последняя строка."""
        found = re.findall(r"ERRCONNECT_[A-Z_]+|ERRINFO_[A-Z_]+", log)
        if found:
            return found[-1].replace("ERRCONNECT_", "")
        rows = [" ".join(r.split()) for r in log.splitlines() if r.strip()]
        return (rows[-1][-60:] if rows else "без сообщения")

    @staticmethod
    def explain(log: str, password: str) -> str:
        """Причина сбоя по журналу клиента RDP. Пароль в текст не попадает."""
        text = log.replace(password, "***") if password else log
        low = text.lower()
        rules = [
            (("logon_failure", "logon failure", "wrong_password", "account_locked", "status_logon"), "неверный логин или пароль"),
            (("account_restriction", "account_disabled", "password_expired", "password_must_change"), "учётная запись отключена или пароль нужно сменить"),
            (("connection refused", "econnrefused"), "сервер отклонил подключение: проверьте порт"),
            (("timed out", "timeout", "unreachable", "dns", "name or service not known"), "сервер не отвечает с нашей стороны: проверьте адрес и порт и открыт ли доступ для сервера системы"),
            (("wait_for_activation", "post_connect failed", "activation"), "сервер принял вход, но оборвал соединение при подготовке сеанса (ограничения сервера на сеанс, цвет или экран)"),
            (("connect_transport_failed", "transport_failed", "connect_failed", "tls_connect", "negotiat"), "сервер оборвал соединение при выборе способа защиты (TLS/NLA): нужны другие настройки на стороне терминала"),
            (("credssp", "nla", "authentication failure", "security negotiation", "connect_security"), "ошибка проверки подлинности (NLA/CredSSP): сервер требует другой способ входа"),
            (("certificate", "cert_"), "проблема с сертификатом сервера"),
        ]
        for keys, msg in rules:
            if any(k in low for k in keys):
                return msg
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return ("последнее сообщение клиента: " + lines[-1][:160]) if lines else "клиент RDP закрылся без сообщения"

    @staticmethod
    def _wait_display(display: int) -> bool:
        sock = Path(f"/tmp/.X11-unix/X{display}")
        for _ in range(50):
            if sock.exists():
                return True
            time.sleep(0.1)
        return False

    def is_open(self, key: int) -> bool:
        s = self._sessions.get(key)
        return bool(s and s.rdp.poll() is None)

    def _env(self, s: _Session) -> dict:
        return {**os.environ, "DISPLAY": f":{s.display}"}

    def _get(self, key: int) -> _Session:
        s = self._sessions.get(key)
        if s is None:
            raise BrowserError("Терминал не открыт: нажмите «Подключиться»")
        if s.rdp.poll() is not None:
            self.close(key)
            raise BrowserError("Подключение к терминалу завершилось: проверьте адрес, логин и пароль и подключитесь снова")
        s.used = time.time()
        return s

    @staticmethod
    def client_home() -> dict:
        """Домашняя папка клиента RDP. Сервер выдаёт клиенту лицензию (CAL), и клиент обязан её сохранить:
        без записываемой папки соединение обрывается на этапе лицензирования. Папка постоянная, чтобы лицензия
        не запрашивалась заново (на терминалах с лицензиями на устройство это тратит их количество)."""
        from .config import settings

        home = Path(settings.files_dir).resolve().parent / "rdp-home"
        try:
            (home / ".config" / "freerdp").mkdir(parents=True, exist_ok=True)
        except OSError:
            home = Path("/tmp/rdp-home")
            (home / ".config" / "freerdp").mkdir(parents=True, exist_ok=True)
        return {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config")}

    def _step(self, key: int, text: str, state: str | None = None, error: str = "") -> None:
        p = self.progress.setdefault(key, {"state": "connecting", "steps": [], "error": ""})
        if text:
            p["steps"].append({"at": time.strftime("%H:%M:%S"), "text": text})
        if state:
            p["state"] = state
        if error:
            p["error"] = error

    def start(self, key: int, host: str, port: int, user: str, password: str, domain: str = "") -> None:
        """Подключение в фоне: окно показывает шаги и картинку виртуального экрана по ходу дела."""
        import threading

        if self.progress.get(key, {}).get("state") == "connecting":
            return
        self.progress[key] = {"state": "connecting", "steps": [], "error": ""}

        def work() -> None:
            try:
                self.open(key, host, port, user, password, domain)
            except BrowserError as exc:
                self._step(key, "", "error", str(exc))
            except Exception:  # noqa: BLE001
                log.exception("Сбой подключения RDP")
                self._step(key, "", "error", "Внутренняя ошибка подключения")

        threading.Thread(target=work, daemon=True, name=f"rdp-open-{key}").start()

    def status(self, key: int) -> dict:
        p = dict(self.progress.get(key) or {"state": "idle", "steps": [], "error": ""})
        if p["state"] == "ready" and not self.is_open(key):
            p = {"state": "idle", "steps": [], "error": ""}
        p["preview"] = key in self._pending
        return p

    def _precheck(self, host: str, port: int) -> None:
        import socket

        try:
            socket.create_connection((host, port), timeout=6).close()
        except OSError as exc:
            ip = self.egress_ip()
            raise BrowserError(
                f"Порт {port} на {host} не отвечает нашему серверу ({type(exc).__name__}). "
                + (f"Откройте доступ для адреса {ip}: с него идут наши подключения. " if ip else "")
                + "Если адрес уже добавлен, проверьте порт и что терминал включён"
            ) from None

    @staticmethod
    def egress_ip() -> str:
        try:
            import httpx

            return httpx.get("https://api.ipify.org", timeout=4.0).text.strip()[:45]
        except Exception:  # noqa: BLE001
            return ""

    def open(self, key: int, host: str, port: int, user: str, password: str, domain: str = "") -> dict:
        self._reap()
        self.close(key)
        self._step(key, f"Проверяю, отвечает ли {host}:{port}")
        try:
            self.precheck(host, port)
        except BrowserError as exc:
            self._step(key, "Порт недоступен", "error", str(exc))
            raise
        self._step(key, "Порт отвечает")
        used = {s.display for s in self._sessions.values()}
        display = next(d for d in range(FIRST_DISPLAY, FIRST_DISPLAY + 50) if d not in used)
        try:
            xvfb = self.spawn(["Xvfb", f":{display}", "-screen", "0", f"{W}x{H}x24", "-nolisten", "tcp"])
        except OSError:
            raise BrowserError("На сервере не установлен Xvfb: пересоберите систему") from None
        if not self.wait_display(display):
            xvfb.terminate()
            raise BrowserError("Не удалось запустить виртуальный экран")
        self._pending[key] = display
        self._step(key, "Виртуальный экран запущен")
        base = [shutil.which("xfreerdp3") or "xfreerdp", f"/v:{host}:{port}", f"/u:{user}", f"/p:{password}", f"/size:{W}x{H}", "/cert:ignore", "-wallpaper"]
        if domain:
            base.append(f"/d:{domain}")
        # Разные серверы требуют разный способ защиты соединения: пробуем по очереди
        full = ["+clipboard", "+auto-reconnect", "/log-level:DEBUG"]  # подробный журнал первой попытки: по нему видно, что сказал сервер
        # облегчённый профиль: меньше цвета, без буфера обмена и лишних эффектов (некоторые серверы рвут соединение на этапе подготовки сеанса)
        light = ["/bpp:16", "-themes", "-aero", "-fonts", "/network:lan", "/log-level:INFO"]
        variants = [
            full,
            light,
            light + ["/sec:nla"],
            light + ["/sec:tls", "/tls:seclevel:0"],
            light + ["/sec:rdp"],
            ["/bpp:16", "-clipboard", "-themes", "-aero", "-fonts", "/network:lan", "/size:1024x768", "/log-level:INFO"],
        ]
        logs: list[str] = []
        rdp = None
        for i, extra in enumerate(variants):
            logpath = Path(f"/tmp/rdp-{display}-{i}.log")
            self._step(key, f"Подключаюсь, набор настроек {i + 1} из {len(variants)}")
            try:
                rdp = self.spawn_logged(base + extra, {**os.environ, "DISPLAY": f":{display}", **self.client_home()}, str(logpath))
            except OSError:
                xvfb.terminate()
                raise BrowserError("На сервере не установлен клиент RDP (xfreerdp): пересоберите систему") from None
            for _ in range(16 if i == 0 else 10):  # ошибка входа или недоступный сервер приходят не сразу
                time.sleep(0.5)
                if rdp.poll() is not None:
                    break
            if rdp.poll() is None:
                break
            try:
                logs.append(logpath.read_text(errors="replace")[-30000 if i == 0 else -3000:])
            except OSError:
                logs.append("")
            if self.explain(logs[-1], password).startswith("неверный логин"):
                break  # другой способ защиты пароль не исправит
        if rdp is None or rdp.poll() is not None:
            xvfb.terminate()
            self._pending.pop(key, None)
            reason = self.explain(logs[0] if logs else "", password)
            if len(logs) > 1 and not reason.startswith("неверный логин"):
                reason += f" (пробовали {len(logs)} наборов настроек соединения)"
            self.last_log[key] = (logs[0] if logs else "").replace(password, "***")[-9000:] if password else (logs[0] if logs else "")[-9000:]
            raw = self.tail(logs[0] if logs else "", password, 2)
            codes = ", ".join(f"{n + 1}: {self.code(lg)}" for n, lg in enumerate(logs))
            ip = self.egress_ip()
            raise BrowserError("Не удалось подключиться к терминалу: " + reason + (f". Коды по наборам настроек: {codes}" if logs else "") + (f". Ответ клиента при первой попытке: {raw}" if raw else "") + (f". Адрес нашего сервера: {ip}" if ip else ""))
        self._sessions[key] = _Session(display, xvfb, rdp)
        self._pending.pop(key, None)
        self._step(key, "Подключено", "ready")
        return {"width": W, "height": H}

    def screenshot(self, key: int) -> bytes:
        if key not in self._sessions and key in self._pending:
            display = self._pending[key]
        else:
            display = self._get(key).display
        try:
            return self.run(["import", "-display", f":{display}", "-window", "root", "-quality", "70", "jpeg:-"], timeout=15)
        except (subprocess.SubprocessError, OSError):
            raise BrowserError("Не удалось получить картинку терминала") from None

    def _xdo(self, key: int, *args: str) -> None:
        s = self._get(key)
        try:
            self.run(["xdotool", *args], env=self._env(s), timeout=10)
        except (subprocess.SubprocessError, OSError):
            raise BrowserError("Не удалось передать ввод в терминал") from None

    def click(self, key: int, x: float, y: float, button: str = "left", double: bool = False) -> None:
        if not (0 <= x <= W and 0 <= y <= H):
            raise BrowserError("Клик за пределами окна")
        num = {"left": "1", "middle": "2", "right": "3"}.get(button)
        if num is None:
            raise BrowserError("Неизвестная кнопка мыши")
        self._xdo(key, "mousemove", str(int(x)), str(int(y)))
        self._xdo(key, "click", *(["--repeat", "2", "--delay", "80"] if double else []), num)

    def type_text(self, key: int, text: str) -> None:
        self._xdo(key, "type", "--delay", "20", "--", text)

    def press(self, key: int, name: str) -> None:
        if not valid_key(name):
            raise BrowserError("Эта клавиша не поддерживается")
        self._xdo(key, "key", to_xdotool(name))

    def scroll(self, key: int, dy: float) -> None:
        steps = max(1, min(10, int(abs(dy) // 100) or 1))
        self._xdo(key, "click", "--repeat", str(steps), "5" if dy > 0 else "4")

    def close(self, key: int) -> None:
        s = self._sessions.pop(key, None)
        if s is None:
            return
        for p in (s.rdp, s.xvfb):
            try:
                p.terminate()
            except OSError:
                pass

    def _reap(self) -> None:
        now = time.time()
        for k in [k for k, s in self._sessions.items() if now - s.used > IDLE_SECONDS or s.rdp.poll() is not None]:
            self.close(k)


rdp_manager = RdpManager()
