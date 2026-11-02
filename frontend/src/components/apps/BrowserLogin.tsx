import { FormEvent, MouseEvent, useCallback, useEffect, useRef, useState } from "react";
import { api, getToken } from "../../api";

interface Props {
  supplierId: number;
  name: string;
  onClose: (saved: boolean) => void;
}

const KEYS: [string, string][] = [["Enter", "Enter"], ["Tab", "Tab"], ["Backspace", "Стереть"], ["Escape", "Esc"]];

/** Окно браузера с сервера: владелец сам входит в кабинет, пароль уходит прямо в страницу и нигде не сохраняется. */
export default function BrowserLogin({ supplierId, name, onClose }: Props) {
  const base = `/returns/suppliers/${supplierId}/browser`;
  const [src, setSrc] = useState("");
  const [size, setSize] = useState<{ width: number; height: number }>({ width: 1100, height: 700 });
  const [state, setState] = useState<"opening" | "ready" | "error">("opening");
  const [error, setError] = useState("");
  const [text, setText] = useState("");
  const [secret, setSecret] = useState(true);
  const [saved, setSaved] = useState(false);
  const [info, setInfo] = useState("");
  const alive = useRef(true);
  const img = useRef<HTMLImageElement>(null);

  const fail = (e: unknown) => setError(e instanceof Error ? e.message : "Ошибка");

  const frame = useCallback(async () => {
    const res = await fetch(`/api${base}/screen`, { headers: { Authorization: `Bearer ${getToken() ?? ""}` } });
    if (!res.ok) throw new Error((await res.json().catch(() => ({ detail: "Нет изображения" }))).detail);
    const url = URL.createObjectURL(await res.blob());
    setSrc((old) => {
      if (old) URL.revokeObjectURL(old);
      return url;
    });
  }, [base]);

  useEffect(() => {
    alive.current = true;
    api<{ width: number; height: number }>(`${base}/open`, { method: "POST" })
      .then((s) => {
        setSize(s);
        setState("ready");
      })
      .catch((e) => {
        fail(e);
        setState("error");
      });
    return () => {
      alive.current = false;
    };
  }, [base]);

  useEffect(() => {
    if (state !== "ready") return;
    let timer: number | undefined;
    const tick = async () => {
      try {
        await frame();
        const i = await api<{ url: string; title: string }>(`${base}/info`);
        if (alive.current) setInfo(`${i.title} · ${i.url}`);
      } catch (e) {
        if (alive.current) fail(e);
      }
      if (alive.current) timer = window.setTimeout(tick, 1000);
    };
    tick();
    return () => window.clearTimeout(timer);
  }, [state, frame, base]);

  const act = async (path: string, body: unknown) => {
    try {
      await api(`${base}/${path}`, { method: "POST", body });
      setError("");
      await frame();
    } catch (e) {
      fail(e);
    }
  };

  function click(e: MouseEvent<HTMLImageElement>) {
    const r = e.currentTarget.getBoundingClientRect();
    void act("click", { x: ((e.clientX - r.left) / r.width) * size.width, y: ((e.clientY - r.top) / r.height) * size.height });
  }

  function send(e: FormEvent) {
    e.preventDefault();
    if (!text) return;
    const t = text;
    setText("");
    void act("type", { text: t });
  }

  async function save() {
    try {
      await api(`${base}/save`, { method: "POST" });
      setSaved(true);
      setError("");
    } catch (e) {
      fail(e);
    }
  }

  async function close() {
    try {
      await api(`${base}/close`, { method: "POST" });
    } catch {
      /* окно могло уже закрыться по таймауту */
    }
    onClose(saved);
  }

  return (
    <div className="modal-bg">
      <div className="modal browser-modal" role="dialog" aria-label={`Вход в кабинет: ${name}`}>
        <h3>Вход в кабинет: {name}</h3>
        <p className="muted small">
          Это браузер на сервере. Кликните по полю, введите логин и пароль ниже, войдите в кабинет, затем нажмите «Сохранить вход». Пароль в системе не хранится: сохраняется только сессия.
        </p>
        {state === "opening" && <p className="muted">Открываю кабинет…</p>}
        {error && <p className="error">{error}</p>}
        {state === "ready" && (
          <>
            <div className="browser-frame">
              {src ? <img ref={img} src={src} alt="Окно браузера" onClick={click} onWheel={(e) => void act("scroll", { dy: e.deltaY })} draggable={false} /> : <p className="muted">Загружаю картинку…</p>}
            </div>
            {info && <p className="muted small">{info}</p>}
            <form className="browser-input" onSubmit={send}>
              <input
                type={secret ? "password" : "text"}
                autoComplete="off"
                placeholder="Текст для выбранного поля (логин или пароль)"
                value={text}
                onChange={(e) => setText(e.target.value)}
              />
              <button type="submit" disabled={!text}>Ввести</button>
              <label className="muted small"><input type="checkbox" checked={secret} onChange={(e) => setSecret(e.target.checked)} /> скрывать</label>
            </form>
            <div className="row">
              {KEYS.map(([k, label]) => (
                <button key={k} type="button" className="ghost" onClick={() => void act("key", { key: k })}>{label}</button>
              ))}
            </div>
          </>
        )}
        <div className="modal-actions">
          {saved && <span className="good">Вход сохранён</span>}
          <span className="grow" />
          <button type="button" className="primary" onClick={save} disabled={state !== "ready"}>Сохранить вход</button>
          <button type="button" onClick={close}>Закрыть</button>
        </div>
      </div>
    </div>
  );
}
