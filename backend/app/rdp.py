"""Удалённый рабочий стол (RDP) на сервере: Xvfb + xfreerdp, картинка и ввод через xdotool.

Пароль берётся из зашифрованного подключения компании, наружу не отдаётся и не пишется в журнал.
Команды с паролем в ошибки и логи не попадают.
"""

import logging
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
        self.run = lambda args, env=None, timeout=10: subprocess.run(args, env=env, capture_output=True, timeout=timeout, check=True).stdout
        self.wait_display = self._wait_display

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

    def open(self, key: int, host: str, port: int, user: str, password: str, domain: str = "") -> dict:
        self._reap()
        self.close(key)
        used = {s.display for s in self._sessions.values()}
        display = next(d for d in range(FIRST_DISPLAY, FIRST_DISPLAY + 50) if d not in used)
        try:
            xvfb = self.spawn(["Xvfb", f":{display}", "-screen", "0", f"{W}x{H}x24", "-nolisten", "tcp"])
        except OSError:
            raise BrowserError("На сервере не установлен Xvfb: пересоберите систему") from None
        if not self.wait_display(display):
            xvfb.terminate()
            raise BrowserError("Не удалось запустить виртуальный экран")
        args = [shutil.which("xfreerdp3") or "xfreerdp", f"/v:{host}:{port}", f"/u:{user}", f"/p:{password}", f"/size:{W}x{H}", "/cert:ignore", "+clipboard", "+auto-reconnect", "-wallpaper"]
        if domain:
            args.append(f"/d:{domain}")
        try:
            rdp = self.spawn(args, {**os.environ, "DISPLAY": f":{display}"})
        except OSError:
            xvfb.terminate()
            raise BrowserError("На сервере не установлен клиент RDP (xfreerdp): пересоберите систему") from None
        self._sessions[key] = _Session(display, xvfb, rdp)
        time.sleep(1.5)
        if rdp.poll() is not None:
            self.close(key)
            raise BrowserError("Не удалось подключиться к терминалу: проверьте адрес, порт, логин и пароль")
        return {"width": W, "height": H}

    def screenshot(self, key: int) -> bytes:
        s = self._get(key)
        try:
            return self.run(["import", "-display", f":{s.display}", "-window", "root", "-quality", "70", "jpeg:-"], timeout=15)
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
