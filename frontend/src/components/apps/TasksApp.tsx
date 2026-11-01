import { useCallback, useEffect, useState } from "react";
import { api, formatDate } from "../../api";
import type { TasksResponse } from "../../types";

const STATUS: Record<number, string> = { 1: "Новая", 2: "Ждёт", 3: "В работе", 4: "Ждёт проверки", 6: "Отложена" };

function deadlineText(days: number | null, overdue: boolean): string {
  if (days === null) return "без срока";
  if (overdue) return days === 0 ? "просрочена сегодня" : `просрочена на ${Math.abs(days)} дн`;
  if (days === 0) return "сегодня";
  if (days === 1) return "завтра";
  return `через ${days} дн`;
}

export default function TasksApp() {
  const [data, setData] = useState<TasksResponse | null>(null);
  const [error, setError] = useState("");
  const [syncing, setSyncing] = useState(false);
  const [onlyProblems, setOnlyProblems] = useState(false);
  const [person, setPerson] = useState("");

  const load = useCallback(() => {
    api<TasksResponse>("/tasks")
      .then((r) => {
        setData(r);
        setError("");
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 60_000);
    return () => clearInterval(t);
  }, [load]);

  async function sync() {
    setSyncing(true);
    try {
      await api("/tasks/sync", { method: "POST" });
      setError("");
      load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Не удалось синхронизировать");
    } finally {
      setSyncing(false);
    }
  }

  const items = (data?.items ?? []).filter(
    (t) => (!person || t.responsible === person) && (!onlyProblems || t.overdue || (t.days_left !== null && t.days_left <= 3)),
  );

  return (
    <div className="app-pad">
      <div className="app-head">
        <div>
          <h2>Задачи Битрикс24</h2>
          <p className="muted">Открытые задачи выбранных сотрудников. Обновляются автоматически каждые 15 минут.</p>
        </div>
        <button onClick={sync} disabled={syncing}>{syncing ? "Обновляем…" : "Обновить сейчас"}</button>
      </div>
      {error && <div className="error">{error}</div>}
      {data && (
        <>
          <div className="stats">
            <div className="stat"><b>{data.total_open}</b><span>открыто</span></div>
            <div className={`stat ${data.overdue ? "bad" : "good"}`}><b>{data.overdue}</b><span>просрочено</span></div>
            <div className="stat"><b>{data.due_today}</b><span>сегодня</span></div>
            <div className="stat"><b>{data.due_soon}</b><span>в ближайшие 3 дня</span></div>
            <div className="stat"><b>{data.no_deadline}</b><span>без срока</span></div>
          </div>
          <div className="integ-tools">
            <select value={person} onChange={(e) => setPerson(e.target.value)}>
              <option value="">Все сотрудники</option>
              {data.people.map((p) => (
                <option key={p.name} value={p.name}>{p.name} ({p.open}{p.overdue ? `, просрочено ${p.overdue}` : ""})</option>
              ))}
            </select>
            <button className={`chip${onlyProblems ? " on" : ""}`} onClick={() => setOnlyProblems((v) => !v)}>
              Только горящие
            </button>
          </div>
          {data.total_open === 0 ? (
            <p className="muted">
              Задач нет. Если Битрикс24 ещё не подключён, укажите вебхук и id сотрудников в Битрикс24 в разделе «Сотрудники».
            </p>
          ) : (
            <table className="files-table">
              <thead>
                <tr><th>Задача</th><th>Ответственный</th><th>Статус</th><th>Срок</th></tr>
              </thead>
              <tbody>
                {items.map((t) => (
                  <tr key={t.id}>
                    <td>{t.title}</td>
                    <td>{t.responsible}</td>
                    <td>{STATUS[t.status] ?? t.status}</td>
                    <td className={t.overdue ? "late" : ""} title={formatDate(t.deadline)}>
                      {deadlineText(t.days_left, t.overdue)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </div>
  );
}
