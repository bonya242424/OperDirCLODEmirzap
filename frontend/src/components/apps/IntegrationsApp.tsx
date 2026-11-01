import { useCallback, useEffect, useMemo, useState } from "react";
import { api, formatAgo, formatDate } from "../../api";
import ConnectionsPanel from "./ConnectionsPanel";
import type { Integration, IntegrationStatus, IntegrationsResponse } from "../../types";

const STATUS_LABEL: Record<IntegrationStatus, string> = {
  ok: "Работает",
  warning: "Внимание",
  error: "Ошибка",
  not_configured: "Не настроено",
  unknown: "Не проверялось",
};

type Filter = "all" | "connected" | "idle" | "problems";

const FILTERS: [Filter, string][] = [
  ["all", "Все"],
  ["connected", "Подключено"],
  ["idle", "Не подключено"],
  ["problems", "Требуют внимания"],
];

function matches(i: Integration, f: Filter): boolean {
  if (f === "connected") return i.status === "ok";
  if (f === "idle") return i.status === "not_configured" || i.status === "unknown";
  if (f === "problems") return i.status === "error" || i.status === "warning";
  return true;
}

export default function IntegrationsApp() {
  const [tab, setTab] = useState<"systems" | "connections">("systems");
  const [filter, setFilter] = useState<Filter>("all");
  const [search, setSearch] = useState("");
  const [data, setData] = useState<IntegrationsResponse | null>(null);
  const [error, setError] = useState("");
  const [checking, setChecking] = useState<string | null>(null);

  const load = useCallback(() => {
    api<IntegrationsResponse>("/integrations")
      .then((r) => {
        setData(r);
        setError("");
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, 60_000);
    return () => clearInterval(timer);
  }, [load]);

  async function checkOne(key: string) {
    setChecking(key);
    try {
      const item = await api<Integration>(`/integrations/${key}/check`, { method: "POST" });
      setData((d) => (d ? { ...d, items: d.items.map((i) => (i.key === key ? item : i)) } : d));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Ошибка проверки");
    } finally {
      setChecking(null);
    }
  }

  async function checkAll() {
    setChecking("all");
    try {
      setData(await api<IntegrationsResponse>("/integrations/check", { method: "POST" }));
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Ошибка проверки");
    } finally {
      setChecking(null);
    }
  }

  const groups = useMemo(() => {
    const q = search.trim().toLowerCase();
    const map = new Map<string, Integration[]>();
    for (const i of data?.items ?? []) {
      if (!matches(i, filter)) continue;
      if (q && !`${i.name} ${i.description} ${i.category}`.toLowerCase().includes(q)) continue;
      map.set(i.category, [...(map.get(i.category) ?? []), i]);
    }
    return [...map.entries()];
  }, [data, filter, search]);

  return (
    <div className="app-pad">
      <div className="app-head">
        <div>
          <h2>Интеграции</h2>
          {data && (
            <p className="muted">
              Состояние проверяется автоматически раз в{" "}
              {data.check_interval_minutes === 60 ? "час" : `${data.check_interval_minutes} мин`}.
            </p>
          )}
        </div>
        <button onClick={checkAll} disabled={checking !== null}>
          {checking === "all" ? "Проверяем…" : "Проверить все"}
        </button>
      </div>
      <div className="chips tabs">
        <button className={`chip${tab === "systems" ? " on" : ""}`} onClick={() => setTab("systems")}>Системы компании</button>
        <button className={`chip${tab === "connections" ? " on" : ""}`} onClick={() => setTab("connections")}>Подключения: компания и сотрудники</button>
      </div>
      {tab === "connections" && <ConnectionsPanel />}
      {error && tab === "systems" && <div className="error">{error}</div>}
      {tab === "systems" && !data && !error && <p className="muted">Загрузка…</p>}
      {tab === "systems" && (<>
      <div className="integ-tools">
        <input
          type="search"
          placeholder="Поиск по интеграциям…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <div className="chips">
          {FILTERS.map(([key, label]) => (
            <button key={key} className={`chip${filter === key ? " on" : ""}`} onClick={() => setFilter(key)}>
              {label}
            </button>
          ))}
        </div>
      </div>
      {data && groups.length === 0 && <p className="muted">Ничего не найдено</p>}
      {groups.map(([category, items]) => (
        <section key={category}>
          <h3 className="cat-title">{category}</h3>
          <div className="cards">
            {items.map((i) => (
          <div key={i.key} className="card">
            <div className="card-top">
              <strong>{i.name}</strong>
              <span className={`badge ${i.status}`}>{STATUS_LABEL[i.status] ?? i.status}</span>
            </div>
            <p className="muted">{i.description}</p>
            {i.message && <p className="card-message">{i.message}</p>}
            <dl>
              <dt>Последняя проверка</dt>
              <dd title={formatDate(i.last_check_at)}>{formatAgo(i.last_check_at)}</dd>
              {i.has_sync && (
                <>
                  <dt>Последняя выгрузка данных</dt>
                  <dd title={formatDate(i.last_sync_at)}>
                    {i.last_sync_at ? `${formatDate(i.last_sync_at)} (${formatAgo(i.last_sync_at)})` : "ещё не было"}
                  </dd>
                </>
              )}
            </dl>
            <button onClick={() => checkOne(i.key)} disabled={checking !== null}>
              {checking === i.key ? "Проверяем…" : "Проверить сейчас"}
            </button>
          </div>
        )) }
          </div>
        </section>
      ))}
      </>)}
    </div>
  );
}
