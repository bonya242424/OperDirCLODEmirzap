import { FormEvent, useCallback, useEffect, useState } from "react";
import { api, formatAgo } from "../../api";
import type { Connection, ConnectionKind } from "../../types";

const STATUS_LABEL: Record<string, string> = {
  ok: "Работает",
  warning: "Внимание",
  error: "Ошибка",
  not_configured: "Не настроено",
  unknown: "Не проверялось",
};

export default function ConnectionsPanel({ startKind }: { startKind?: string }) {
  const [items, setItems] = useState<Connection[]>([]);
  const [kinds, setKinds] = useState<ConnectionKind[]>([]);
  const [error, setError] = useState("");
  const [adding, setAdding] = useState(false);
  const [kind, setKind] = useState("telegram");
  const [scope, setScope] = useState<"personal" | "company">("personal");
  const [title, setTitle] = useState("");
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<number | "form" | null>(null);
  const [editing, setEditing] = useState<Connection | null>(null);
  const [editTitle, setEditTitle] = useState("");
  const [editValues, setEditValues] = useState<Record<string, string>>({});
  const [loaded, setLoaded] = useState(false);

  const load = useCallback(() => {
    api<Connection[]>("/connections")
      .then((r) => {
        setItems(r);
        setLoaded(true);
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  function startEdit(c: Connection) {
    setAdding(false);
    setEditing(c);
    setEditTitle(c.title);
    setEditValues({});
  }

  // переход из карточки системы «Модель ИИ»: открыть ввод ключа или изменение существующего
  useEffect(() => {
    if (!startKind || !loaded) return;
    const existing = items.find((c) => c.kind === startKind && c.scope === "company");
    if (existing) startEdit(existing);
    else {
      setKind(startKind);
      setScope("company");
      setAdding(true);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [startKind, loaded]);

  async function saveEdit(e: FormEvent) {
    e.preventDefault();
    if (!editing) return;
    setBusy(editing.id);
    try {
      await api(`/connections/${editing.id}`, { method: "PATCH", body: { title: editTitle, config: editValues } });
      setEditing(null);
      setError("");
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось сохранить");
    } finally {
      setBusy(null);
    }
  }

  useEffect(() => {
    load();
    api<ConnectionKind[]>("/connections/kinds").then(setKinds).catch(() => {});
    const t = setInterval(load, 60_000);
    return () => clearInterval(t);
  }, [load]);

  const spec = kinds.find((k) => k.key === kind);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy("form");
    try {
      await api("/connections", { method: "POST", body: { kind, scope, title, config: values } });
      setAdding(false);
      setTitle("");
      setValues({});
      setError("");
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось добавить");
    } finally {
      setBusy(null);
    }
  }

  async function act(id: number, path: string, method: string) {
    setBusy(id);
    try {
      await api(`/connections/${id}${path}`, { method });
      setError("");
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Ошибка");
    } finally {
      setBusy(null);
    }
  }

  const groups = new Map<string, Connection[]>();
  for (const c of items) groups.set(c.owner_name, [...(groups.get(c.owner_name) ?? []), c]);

  return (
    <div>
      <div className="app-head">
        <p className="muted">
          Подключения компании общие, личные принадлежат сотруднику. Пока все видят все подключения; пароли и токены
          после сохранения не показываются никому.
        </p>
        <button onClick={() => setAdding((v) => !v)}>{adding ? "Отмена" : "Добавить подключение"}</button>
      </div>
      {error && <div className="error">{error}</div>}
      {adding && (
        <form className="card conn-form" onSubmit={submit}>
          <label>
            Тип
            <select value={kind} onChange={(e) => { setKind(e.target.value); setValues({}); }}>
              {kinds.map((k) => (
                <option key={k.key} value={k.key}>{k.name}</option>
              ))}
            </select>
          </label>
          <label>
            Чьё
            <select value={scope} onChange={(e) => setScope(e.target.value as "personal" | "company")}>
              <option value="personal">Моё личное</option>
              <option value="company">Компании</option>
            </select>
          </label>
          <label>
            Название
            <input value={title} placeholder={spec?.name} onChange={(e) => setTitle(e.target.value)} />
          </label>
          {spec?.fields.map((f) => (
            <label key={f.name}>
              {f.label}
              <input
                type={f.secret ? "password" : "text"}
                autoComplete="off"
                placeholder={f.default}
                value={values[f.name] ?? ""}
                onChange={(e) => setValues((v) => ({ ...v, [f.name]: e.target.value }))}
              />
            </label>
          ))}
          <button type="submit" disabled={busy === "form"}>{busy === "form" ? "Проверяем…" : "Сохранить и проверить"}</button>
        </form>
      )}
      {editing && (() => {
        const es = kinds.find((k) => k.key === editing.kind);
        return (
          <form className="form" onSubmit={saveEdit}>
            <h3>Изменить: {editing.kind_name}</h3>
            <label>
              Название
              <input value={editTitle} onChange={(e) => setEditTitle(e.target.value)} />
            </label>
            {es?.fields.map((f) => (
              <label key={f.name}>
                {f.label}
                <input
                  type={f.secret ? "password" : "text"}
                  autoComplete="off"
                  placeholder={f.secret ? (editing.secrets_set.includes(f.name) ? "Задано. Оставьте пустым, чтобы не менять" : "Не задано") : editing.values[f.name] ?? f.default}
                  value={editValues[f.name] ?? (f.secret ? "" : editing.values[f.name] ?? "")}
                  onChange={(e) => setEditValues((v) => ({ ...v, [f.name]: e.target.value }))}
                />
              </label>
            ))}
            <div className="row">
              <button type="submit" disabled={busy === editing.id}>{busy === editing.id ? "Проверяем…" : "Сохранить и проверить"}</button>
              <button type="button" className="ghost" onClick={() => setEditing(null)}>Отмена</button>
            </div>
          </form>
        );
      })()}
      {items.length === 0 && !error && <p className="muted">Подключений пока нет.</p>}
      {[...groups.entries()].map(([owner, list]) => (
        <section key={owner}>
          <h3 className="cat-title">{owner}</h3>
          <div className="cards">
            {list.map((c) => (
              <div key={c.id} className="card">
                <div className="card-top">
                  <strong>{c.title}</strong>
                  <span className={`badge ${c.status}`}>{STATUS_LABEL[c.status] ?? c.status}</span>
                </div>
                <p className="muted">{c.kind_name}</p>
                {c.message && <p className="card-message">{c.message}</p>}
                <dl>
                  <dt>Последняя проверка</dt>
                  <dd>{formatAgo(c.last_check_at)}</dd>
                </dl>
                <div className="row">
                  <button disabled={busy !== null} onClick={() => act(c.id, "/check", "POST")}>Проверить</button>
                  {c.can_edit && <button disabled={busy !== null} onClick={() => startEdit(c)}>Изменить</button>}
                  {c.can_edit && (
                    <button
                      className="ghost"
                      disabled={busy !== null}
                      onClick={() => window.confirm("Удалить подключение?") && act(c.id, "", "DELETE")}
                    >
                      Удалить
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}
