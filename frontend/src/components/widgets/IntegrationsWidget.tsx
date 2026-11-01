import type { IntegrationsResponse } from "../../types";

export default function IntegrationsWidget({
  data,
  onOpen,
}: {
  data: IntegrationsResponse | null;
  onOpen: () => void;
}) {
  const items = data?.items ?? [];
  const problems = items.filter((i) => i.status === "error" || i.status === "warning");
  const working = items.filter((i) => i.status === "ok").length;
  const idle = items.filter((i) => i.status === "not_configured" || i.status === "unknown").length;

  return (
    <div
      className="widget"
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => e.key === "Enter" && onOpen()}
    >
      <div className="widget-title">Интеграции</div>
      {!data ? (
        <p className="muted">Загрузка…</p>
      ) : (
        <>
          <div className="stats">
            <div className="stat good">
              <b>{working}</b>
              <span>работают</span>
            </div>
            <div className={`stat ${problems.length ? "bad" : ""}`}>
              <b>{problems.length}</b>
              <span>требуют внимания</span>
            </div>
            <div className="stat">
              <b>{idle}</b>
              <span>не настроены</span>
            </div>
          </div>
          {problems.length > 0 ? (
            <ul className="widget-list">
              {problems.slice(0, 3).map((i) => (
                <li key={i.key}>
                  <strong>{i.name}</strong>
                  <span className="muted">{i.message}</span>
                </li>
              ))}
            </ul>
          ) : working === 0 ? (
            <p className="muted">Источники ещё не подключены. Откройте раздел «Интеграции».</p>
          ) : (
            <p className="muted">Проблем нет. Проверка идёт автоматически.</p>
          )}
        </>
      )}
    </div>
  );
}
