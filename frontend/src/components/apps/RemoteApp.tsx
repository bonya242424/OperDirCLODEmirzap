import { FormEvent, useCallback, useEffect, useState } from "react";
import { api } from "../../api";
import type { User } from "../../types";
import RemoteWindow from "./RemoteWindow";

interface Targets {
  browser: { open: boolean };
  rdp: { id: number; title: string; host: string; open: boolean }[];
}

/** Удалённые окна: обычный браузер на сервере и терминалы RDP (например 1С). Только владелец. */
export default function RemoteApp({ me }: { me: User }) {
  const [targets, setTargets] = useState<Targets | null>(null);
  const [error, setError] = useState("");
  const [active, setActive] = useState<{ base: string; title: string; address: boolean; body?: Record<string, unknown> } | null>(null);
  const [url, setUrl] = useState("");

  const load = useCallback(() => {
    api<Targets>("/remote/targets").then(setTargets).catch((e) => setError(e instanceof Error ? e.message : "Ошибка"));
  }, []);
  useEffect(() => {
    if (me.role === "owner") load();
  }, [load, me.role]);

  if (me.role !== "owner") return <div className="placeholder"><p className="muted">Удалённые окна открывает только владелец.</p></div>;

  function openBrowser(e: FormEvent) {
    e.preventDefault();
    const u = url.trim();
    setActive({ base: "/remote/browser", title: "Браузер", address: true, body: u ? { url: /^https?:\/\//.test(u) ? u : `https://${u}` } : {} });
  }

  return (
    <div className="agents">
      <aside className="ag-left">
        <h3>Удалённые окна</h3>
        <p className="muted small">Работа на сервере через систему: обычный браузер и терминал 1С. Окно закрывается само после 15–30 минут без действий.</p>
        <form onSubmit={openBrowser} className="ag-link">
          <input placeholder="Адрес сайта (необязательно)" value={url} onChange={(e) => setUrl(e.target.value)} />
          <button className="primary">Открыть браузер</button>
        </form>
        <h4>Терминалы</h4>
        <ul>
          {targets?.rdp.map((t) => (
            <li key={t.id}>
              <button className={active?.base === `/remote/rdp/${t.id}` ? "on" : ""} onClick={() => setActive({ base: `/remote/rdp/${t.id}`, title: t.title, address: false })}>
                <strong>{t.title}</strong>
                <span className="muted small">{t.host}{t.open ? " · подключён" : ""}</span>
              </button>
            </li>
          ))}
        </ul>
        {targets && targets.rdp.length === 0 && <p className="muted small">Терминалов нет. Добавьте в «Интеграции → Подключения → Добавить подключение → Терминал (RDP)»: адрес, порт, логин и пароль вводятся один раз.</p>}
        {error && <p className="error">{error}</p>}
      </aside>
      <section className="ag-main">
        {active ? (
          <>
            <header><h2>{active.title}</h2></header>
            <RemoteWindow key={active.base + JSON.stringify(active.body ?? {})} base={active.base} openBody={active.body} address={active.address} onClosed={() => { setActive(null); load(); }} />
          </>
        ) : (
          <p className="ag-hint">Откройте браузер или подключитесь к терминалу слева. Подключение к терминалу выполняется само по сохранённым данным.</p>
        )}
      </section>
    </div>
  );
}
