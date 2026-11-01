import { useCallback, useEffect, useState } from "react";
import { api, formatAgo, formatDate } from "../../api";
import type { Integration, IntegrationStatus, IntegrationsResponse } from "../../types";

const STATUS_LABEL: Record<IntegrationStatus, string> = {
  ok: "Работает",
  warning: "Внимание",
  error: "Ошибка",
  not_configured: "Не настроено",
  unknown: "Не проверялось",
};

export default function IntegrationsApp() {
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
      {error && <div className="error">{error}</div>}
      {!data && !error && <p className="muted">Загрузка…</p>}
      <div className="cards">
        {data?.items.map((i) => (
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
        ))}
      </div>
    </div>
  );
}
