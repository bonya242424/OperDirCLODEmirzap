import { FormEvent, useCallback, useEffect, useState } from "react";
import { api, formatAgo } from "../../api";

interface Proxy {
  id: number;
  name: string;
  scheme: "http" | "https" | "socks5";
  host: string;
  port: number;
  username: string;
  has_password: boolean;
  priority: number;
  enabled: boolean;
  last_status: "ok" | "error" | "unknown";
  last_message: string;
  last_ms: number | null;
  last_check_at: string | null;
  can_edit: boolean;
}
interface Form {
  id: number | null;
  name: string;
  scheme: Proxy["scheme"];
  host: string;
  port: string;
  username: string;
  password: string;
  priority: string;
  enabled: boolean;
  had_password: boolean;
}

const blank = (n: number): Form => ({ id: null, name: "", scheme: "http", host: "", port: "", username: "", password: "", priority: String((n + 1) * 10), enabled: true, had_password: false });
const STATUS = { ok: "работает", error: "не отвечает", unknown: "не проверен" };

export default function ProxiesPanel() {
  const [items, setItems] = useState<Proxy[] | null>(null);
  const [form, setForm] = useState<Form | null>(null);
  const [error, setError] = useState("");
  const [checking, setChecking] = useState<number | null>(null);
  const fail = (e: unknown) => setError(e instanceof Error ? e.message : "Ошибка");

  const load = useCallback(() => {
    api<Proxy[]>("/proxies").then((p) => { setItems(p); setError(""); }).catch(fail);
  }, []);
  useEffect(load, [load]);

  async function save(e: FormEvent) {
    e.preventDefault();
    if (!form) return;
    const body: Record<string, unknown> = {
      name: form.name,
      scheme: form.scheme,
      host: form.host,
      port: Number(form.port),
      username: form.username,
      priority: Number(form.priority) || 100,
      enabled: form.enabled,
    };
    if (form.id === null || form.password !== "") body.password = form.password;
    try {
      if (form.id === null) await api("/proxies", { method: "POST", body });
      else await api(`/proxies/${form.id}`, { method: "PUT", body });
      setForm(null);
      load();
    } catch (err) {
      fail(err);
    }
  }

  async function check(p: Proxy) {
    setChecking(p.id);
    try {
      await api(`/proxies/${p.id}/check`, { method: "POST" });
      load();
    } catch (err) {
      fail(err);
    } finally {
      setChecking(null);
    }
  }

  async function remove(p: Proxy) {
    if (!window.confirm(`Удалить прокси ${p.host}:${p.port}?`)) return;
    try {
      await api(`/proxies/${p.id}`, { method: "DELETE" });
      load();
    } catch (err) {
      fail(err);
    }
  }

  return (
    <div className="proxies">
      <div className="app-head">
        <div>
          <h4>Прокси для запросов к ИИ</h4>
          <p className="muted small">
            Сервер стоит в России, поэтому запросы к ИИ идут через эти прокси. Система пробует их по порядку приоритета: если первый не отвечает, берёт следующий. Список общий для всей компании. Пока прокси нет, запросы идут напрямую.
          </p>
        </div>
        <button className="primary" onClick={() => setForm(blank(items?.length ?? 0))}>+ Добавить прокси</button>
      </div>
      {error && <div className="error">{error}</div>}
      {items && items.length === 0 && <div className="ag-hint">Прокси не добавлены. Добавьте хотя бы один, иначе ИИ с российского сервера может не отвечать.</div>}
      <ul className="px-list">
        {items?.map((p) => (
          <li key={p.id} className={`px-item${p.enabled ? "" : " off"}`}>
            <div>
              <strong>{p.name || `${p.host}:${p.port}`}</strong>
              <p className="muted small">
                {p.scheme}://{p.username ? `${p.username}:••••@` : ""}{p.host}:{p.port} · приоритет {p.priority}{!p.enabled ? " · выключен" : ""}
              </p>
              {p.last_message && <p className="muted small">{p.last_message}{p.last_ms !== null ? ` (${p.last_ms} мс)` : ""}</p>}
            </div>
            <div className="px-side">
              <span className={`chipd st-${p.last_status === "ok" ? "done" : p.last_status === "error" ? "failed" : "none"}`}>
                {STATUS[p.last_status]}{p.last_check_at ? ` · ${formatAgo(p.last_check_at)}` : ""}
              </span>
              <div className="ag-actions">
                <button onClick={() => check(p)} disabled={checking === p.id}>{checking === p.id ? "Проверяем…" : "Проверить"}</button>
                {p.can_edit && (
                  <>
                    <button onClick={() => setForm({ id: p.id, name: p.name, scheme: p.scheme, host: p.host, port: String(p.port), username: p.username, password: "", priority: String(p.priority), enabled: p.enabled, had_password: p.has_password })}>Изменить</button>
                    <button className="ghost danger" onClick={() => remove(p)}>Удалить</button>
                  </>
                )}
              </div>
            </div>
          </li>
        ))}
      </ul>

      {form && (
        <div className="modal-bg" onPointerDown={() => setForm(null)}>
          <form className="modal" onSubmit={save} onPointerDown={(e) => e.stopPropagation()}>
            <h3>{form.id === null ? "Новый прокси" : "Прокси"}</h3>
            <input placeholder="Название (необязательно)" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
            <div className="modal-grid">
              <label>
                Тип
                <select value={form.scheme} onChange={(e) => setForm({ ...form, scheme: e.target.value as Proxy["scheme"] })}>
                  <option value="http">HTTP</option>
                  <option value="https">HTTPS</option>
                  <option value="socks5">SOCKS5</option>
                </select>
              </label>
              <label>
                Приоритет
                <input type="number" min={1} value={form.priority} onChange={(e) => setForm({ ...form, priority: e.target.value })} />
              </label>
            </div>
            <div className="modal-grid">
              <label>
                Адрес
                <input required placeholder="proxy.example.com" value={form.host} onChange={(e) => setForm({ ...form, host: e.target.value })} />
              </label>
              <label>
                Порт
                <input required type="number" min={1} max={65535} value={form.port} onChange={(e) => setForm({ ...form, port: e.target.value })} />
              </label>
            </div>
            <div className="modal-grid">
              <label>
                Логин
                <input autoComplete="off" value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} />
              </label>
              <label>
                Пароль
                <input type="password" autoComplete="new-password" placeholder={form.had_password ? "не менять" : ""} value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} />
              </label>
            </div>
            <label className="ag-tool">
              <input type="checkbox" checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />
              <span>Использовать этот прокси</span>
            </label>
            <div className="modal-actions">
              <span className="grow" />
              <button type="button" onClick={() => setForm(null)}>Отмена</button>
              <button type="submit" className="primary">Сохранить</button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}
