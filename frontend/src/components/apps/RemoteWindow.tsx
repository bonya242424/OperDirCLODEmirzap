import { ClipboardEvent, FormEvent, KeyboardEvent, MouseEvent, useCallback, useEffect, useRef, useState } from "react";
import { api, getToken } from "../../api";

interface Props {
  /** Базовый адрес API: /remote/browser или /remote/rdp/3 */
  base: string;
  /** Тело запроса открытия (для браузера адрес страницы) */
  openBody?: Record<string, unknown>;
  address?: boolean;
  onClosed?: () => void;
}

const NAMED: Record<string, string> = {
  Enter: "Enter", Backspace: "Backspace", Tab: "Tab", Escape: "Escape", Delete: "Delete", " ": "Space",
  ArrowDown: "ArrowDown", ArrowUp: "ArrowUp", ArrowLeft: "ArrowLeft", ArrowRight: "ArrowRight",
  Home: "Home", End: "End", PageUp: "PageUp", PageDown: "PageDown", Insert: "Insert", ContextMenu: "ContextMenu", CapsLock: "CapsLock",
};

/** Окно удалённого экрана: картинка с сервера, клики, колесо, клавиатура с компьютера. */
export default function RemoteWindow({ base, openBody, address = false, onClosed }: Props) {
  const [state, setState] = useState<"opening" | "ready" | "error">("opening");
  const [size, setSize] = useState({ width: 1100, height: 700 });
  const [src, setSrc] = useState("");
  const [error, setError] = useState("");
  const [url, setUrl] = useState("");
  const [paste, setPaste] = useState("");
  const [info, setInfo] = useState("");
  const alive = useRef(true);
  const buf = useRef("");
  const timer = useRef<number | undefined>(undefined);
  const box = useRef<HTMLDivElement>(null);

  const fail = (e: unknown) => setError(e instanceof Error ? e.message : "Ошибка");

  const frame = useCallback(async () => {
    const res = await fetch(`/api${base}/screen`, { headers: { Authorization: `Bearer ${getToken() ?? ""}` } });
    if (!res.ok) throw new Error((await res.json().catch(() => ({ detail: "Нет изображения" }))).detail);
    const u = URL.createObjectURL(await res.blob());
    setSrc((old) => {
      if (old) URL.revokeObjectURL(old);
      return u;
    });
  }, [base]);

  useEffect(() => {
    alive.current = true;
    setState("opening");
    setError("");
    api<{ width: number; height: number }>(`${base}/open`, { method: "POST", body: openBody ?? {} })
      .then((s) => {
        setSize(s);
        setState("ready");
        box.current?.focus();
      })
      .catch((e) => {
        fail(e);
        setState("error");
      });
    return () => {
      alive.current = false;
      window.clearTimeout(timer.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [base]);

  useEffect(() => {
    if (state !== "ready") return;
    let t: number | undefined;
    const tick = async () => {
      try {
        await frame();
        if (address) {
          const i = await api<{ url: string; title: string }>(`${base}/info`);
          if (alive.current) setInfo(i.title);
          setUrl((cur) => (document.activeElement?.id === "remote-url" ? cur : i.url));
        }
        if (alive.current) setError("");
      } catch (e) {
        if (alive.current) fail(e);
      }
      if (alive.current) t = window.setTimeout(tick, 900);
    };
    tick();
    return () => window.clearTimeout(t);
  }, [state, frame, base, address]);

  const act = async (path: string, body?: unknown) => {
    try {
      await api(`${base}/${path}`, { method: "POST", body: body ?? {} });
      setError("");
      await frame();
    } catch (e) {
      fail(e);
    }
  };

  const point = (e: MouseEvent<HTMLElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    return { x: ((e.clientX - r.left) / r.width) * size.width, y: ((e.clientY - r.top) / r.height) * size.height };
  };

  const flush = useCallback(() => {
    const text = buf.current;
    buf.current = "";
    if (text) void act("type", { text });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [base]);

  function onKey(e: KeyboardEvent<HTMLDivElement>) {
    if (state !== "ready" || e.nativeEvent.isComposing) return;
    const ctrl = e.ctrlKey || e.metaKey;
    // Ctrl+V: вставку обработает событие paste (берёт текст из буфера обмена компьютера)
    if (ctrl && e.code === "KeyV" && !e.shiftKey) return;
    const mods = [e.ctrlKey && "Control", e.altKey && "Alt", e.metaKey && "Meta"].filter(Boolean) as string[];
    const named = NAMED[e.key];
    if (e.key.length === 1 && mods.length === 0) {
      // обычный ввод, в том числе русская раскладка и заглавные буквы
      e.preventDefault();
      buf.current += e.key;
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(flush, 120);
      return;
    }
    // сочетания считаем по физической клавише, чтобы Ctrl+С на русской раскладке работал как Ctrl+C
    let key = named ?? "";
    if (!key && /^Key[A-Z]$/.test(e.code)) key = e.code.slice(3).toLowerCase();
    else if (!key && /^Digit\d$/.test(e.code)) key = e.code.slice(5);
    else if (!key && /^F([1-9]|1[0-2])$/.test(e.key)) key = e.key;
    else if (!key && e.key.length === 1) key = e.key.toLowerCase();
    if (!key) return;
    if (e.shiftKey && (named || mods.length)) mods.push("Shift");
    e.preventDefault();
    flush();
    void act("key", { key: [...mods, key].join("+") });
  }

  function onPaste(e: ClipboardEvent<HTMLDivElement>) {
    const text = e.clipboardData.getData("text");
    if (!text) return;
    e.preventDefault();
    flush();
    void act("type", { text });
  }

  async function go(e: FormEvent) {
    e.preventDefault();
    const target = /^https?:\/\//.test(url) ? url : `https://${url}`;
    await act("navigate", { url: target });
  }

  async function sendPaste(e: FormEvent) {
    e.preventDefault();
    if (!paste) return;
    const t = paste;
    setPaste("");
    await act("type", { text: t });
  }

  async function disconnect() {
    try {
      await api(`${base}/close`, { method: "POST" });
    } catch {
      /* уже закрыто */
    }
    onClosed?.();
  }

  return (
    <div className="remote">
      {address && (
        <form className="remote-bar" onSubmit={go}>
          <button type="button" className="ghost" onClick={() => void act("history", { action: "back" })} title="Назад">←</button>
          <button type="button" className="ghost" onClick={() => void act("history", { action: "forward" })} title="Вперёд">→</button>
          <button type="button" className="ghost" onClick={() => void act("history", { action: "reload" })} title="Обновить">⟳</button>
          <input id="remote-url" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="Адрес страницы" />
          <button type="submit">Перейти</button>
        </form>
      )}
      {state === "opening" && <p className="muted">Подключаюсь…</p>}
      {error && <p className="error">{error}</p>}
      {state === "ready" && (
        <>
          <div className="remote-view" ref={box} tabIndex={0} onKeyDown={onKey} onPaste={onPaste} role="application" aria-label="Удалённый экран: кликните и печатайте с клавиатуры">
            {src ? (
              <img
                src={src}
                alt="Удалённый экран"
                draggable={false}
                onClick={(e) => { box.current?.focus(); void act("click", point(e)); }}
                onDoubleClick={(e) => void act("click", { ...point(e), double: true })}
                onContextMenu={(e) => { e.preventDefault(); void act("click", { ...point(e), button: "right" }); }}
                onWheel={(e) => void act("scroll", { dy: e.deltaY })}
              />
            ) : (
              <p className="muted">Загружаю картинку…</p>
            )}
          </div>
          <p className="muted small">{info ? `${info} · ` : ""}Кликните по экрану и печатайте: клавиши идут в удалённое окно. Двойной клик и правая кнопка работают. Ctrl+V вставляет текст из буфера вашего компьютера, русская раскладка и сочетания (Ctrl+C, Alt+Tab, F-клавиши) работают. Копирование из удалённого окна к вам не передаётся.</p>
          <form className="remote-bar" onSubmit={sendPaste}>
            <input type="password" autoComplete="off" value={paste} onChange={(e) => setPaste(e.target.value)} placeholder="Вставить текст в выбранное поле" />
            <button type="submit" disabled={!paste}>Ввести</button>
            <button type="button" className="ghost danger" onClick={disconnect}>Отключиться</button>
          </form>
        </>
      )}
      {state === "error" && <button onClick={onClosed}>Назад</button>}
    </div>
  );
}
