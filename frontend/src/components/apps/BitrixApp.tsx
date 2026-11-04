import { useCallback, useEffect, useState } from "react";
import { api } from "../../api";
import type { User } from "../../types";

interface Task {
  id: number;
  title: string;
  status: number;
  status_name: string;
  responsible: string;
  created: string | null;
  closed: string | null;
  deadline: string | null;
}
interface Period {
  count: number;
  by_status: Record<string, number>;
  tasks: Task[];
}
interface EventRow {
  id: number;
  task_id: number;
  title: string;
  kind: string;
  text: string;
  at: string;
  responsible: string;
}
interface Employee {
  bitrix_user_id: number;
  name: string;
}

const KINDS = { any: "Любая дата (создана, закрыта или срок)", created: "Дата создания", closed: "Дата закрытия", deadline: "Срок", changed: "Дата изменения" } as const;
const iso = (d: Date) => d.toISOString().slice(0, 10);
const day = (s: string | null) => (s ? new Date(s).toLocaleDateString("ru-RU") : "—");

/** Задачи Битрикс24 за период и обновления по ним. Только владелец. */
export default function BitrixApp({ me }: { me: User }) {
  const today = new Date();
  const [tab, setTab] = useState<"tasks" | "updates" | "people">("tasks");
  const [ids, setIds] = useState("");
  const [imported, setImported] = useState<{ bitrix_user_id: number; status: string; name: string; needs_email?: boolean }[] | null>(null);
  const [from, setFrom] = useState(iso(new Date(today.getFullYear(), today.getMonth(), 1)));
  const [to, setTo] = useState(iso(today));
  const [kind, setKind] = useState<keyof typeof KINDS>("any");
  const [emp, setEmp] = useState("");
  const [employees, setEmployees] = useState<Employee[]>([]);
  const [data, setData] = useState<Period | null>(null);
  const [events, setEvents] = useState<EventRow[] | null>(null);
  const [days, setDays] = useState(7);
  const [error, setError] = useState("");
  const [info, setInfo] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (me.role === "owner") api<Employee[]>("/bitrix/employees").then(setEmployees).catch(() => undefined);
  }, [me.role]);

  const loadTasks = useCallback(async () => {
    setBusy(true);
    setError("");
    try {
      const q = new URLSearchParams({ date_from: from, date_to: to, kind });
      if (emp) q.set("user_id", emp);
      setData(await api<Period>(`/bitrix/tasks?${q}`));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Ошибка");
    } finally {
      setBusy(false);
    }
  }, [from, to, kind, emp]);

  const loadEvents = useCallback(() => {
    api<EventRow[]>(`/bitrix/events?days=${days}`).then(setEvents).catch((e) => setError(e instanceof Error ? e.message : "Ошибка"));
  }, [days]);
  useEffect(() => {
    if (tab === "updates") loadEvents();
  }, [tab, loadEvents]);

  async function importPeople() {
    setBusy(true);
    setError("");
    try {
      const list = ids.split(/[\s,;]+/).map(Number).filter((n) => Number.isInteger(n) && n > 0);
      setImported(await api("/bitrix/import-users", { method: "POST", body: { ids: list } }));
      api<Employee[]>("/bitrix/employees").then(setEmployees).catch(() => undefined);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Ошибка");
    } finally {
      setBusy(false);
    }
  }

  async function watchNow() {
    setBusy(true);
    setError("");
    setInfo("");
    try {
      const r = await api<{ baseline: boolean; checked: number; events: number }>("/bitrix/watch-now", { method: "POST" });
      setInfo(r.baseline ? `Первая проверка: запомнил состояние ${r.checked} задач. Обновления появятся со следующей проверки.` : `Проверено задач: ${r.checked}, новых обновлений: ${r.events}`);
      loadEvents();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Ошибка");
    } finally {
      setBusy(false);
    }
  }

  if (me.role !== "owner") return <div className="placeholder"><p className="muted">Задачи Битрикс24 видит только владелец.</p></div>;

  return (
    <div className="bitrix-app">
      <div className="kanban-bar">
        <h2>Задачи Битрикс24</h2>
        <div className="row small">
          <button className={tab === "tasks" ? "primary" : "ghost"} onClick={() => setTab("tasks")}>За период</button>
          <button className={tab === "updates" ? "primary" : "ghost"} onClick={() => setTab("updates")}>Обновления</button>
          <button className={tab === "people" ? "primary" : "ghost"} onClick={() => setTab("people")}>Сотрудники из Б24</button>
        </div>
      </div>
      {error && <p className="error">{error}</p>}
      {info && <p className="ok-text">{info}</p>}
      {tab === "tasks" && (
        <>
          <div className="row small">
            <input type="date" value={from} max={to} onChange={(e) => setFrom(e.target.value)} aria-label="С даты" />
            <input type="date" value={to} min={from} onChange={(e) => setTo(e.target.value)} aria-label="По дату" />
            <select value={kind} onChange={(e) => setKind(e.target.value as keyof typeof KINDS)} aria-label="Какая дата">
              {Object.entries(KINDS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
            <select value={emp} onChange={(e) => setEmp(e.target.value)} aria-label="Сотрудник">
              <option value="">Все выбранные сотрудники</option>
              {employees.map((x) => <option key={x.bitrix_user_id} value={x.bitrix_user_id}>{x.name}</option>)}
            </select>
            <button className="primary" onClick={loadTasks} disabled={busy}>{busy ? "Загружаю…" : "Показать задачи"}</button>
          </div>
          {employees.length === 0 && <p className="ag-hint">Не выбрано ни одного сотрудника с id в Битрикс24. Укажите id в карточке сотрудника («Сотрудники»), и здесь появятся его задачи.</p>}
          {data && (
            <>
              <p className="muted small">Задач: <b>{data.count}</b>. {Object.entries(data.by_status).map(([k, v]) => `${k}: ${v}`).join(" · ")}</p>
              <div className="table-wrap">
                <table className="bx-table">
                  <thead><tr><th>№</th><th>Задача</th><th>Статус</th><th>Исполнитель</th><th>Создана</th><th>Срок</th><th>Закрыта</th></tr></thead>
                  <tbody>
                    {data.tasks.map((t) => (
                      <tr key={t.id}>
                        <td>{t.id}</td><td>{t.title}</td><td>{t.status_name}</td><td>{t.responsible}</td><td>{day(t.created)}</td><td>{day(t.deadline)}</td><td>{day(t.closed)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </>
      )}
      {tab === "people" && (
        <>
          <p className="muted small">Впишите id сотрудников из Битрикс24 через запятую или с новой строки. Система возьмёт из Битрикс24 ФИО, должность и почту (если она доступна) и добавит сотрудников. Вход им будет закрыт, пока вы не зададите почту и пароль в «Сотрудниках».</p>
          <textarea rows={3} value={ids} onChange={(e) => setIds(e.target.value)} placeholder="Например: 12, 15, 21" aria-label="id в Битрикс24" />
          <div className="row small">
            <button className="primary" onClick={importPeople} disabled={busy || !ids.trim()}>{busy ? "Добавляю…" : "Добавить из Битрикс24"}</button>
          </div>
          {imported && imported.map((r) => (
            <div key={r.bitrix_user_id} className="assembly-history-item small">
              <strong>#{r.bitrix_user_id}</strong> {r.name}{" "}
              {r.status === "created" ? (r.needs_email ? "— добавлен, нужно задать почту и пароль в «Сотрудниках»" : "— добавлен с почтой из Битрикс24, осталось задать пароль") : r.status === "exists" ? "— уже есть в системе" : "— в Битрикс24 не найден"}
            </div>
          ))}
        </>
      )}
      {tab === "updates" && (
        <>
          <div className="row small">
            <select value={days} onChange={(e) => setDays(Number(e.target.value))} aria-label="За сколько дней">
              <option value={1}>За сутки</option><option value={7}>За неделю</option><option value={30}>За месяц</option>
            </select>
            <button className="ghost" onClick={watchNow} disabled={busy}>{busy ? "Проверяю…" : "Проверить сейчас"}</button>
            <span className="muted small">Система сама проверяет задачи раз в полчаса.</span>
          </div>
          {events && events.length === 0 && <p className="ag-hint">Обновлений пока нет. Первая проверка запоминает текущее состояние задач, изменения появятся после неё.</p>}
          {events && events.map((e) => (
            <div key={e.id} className="assembly-history-item">
              <strong>#{e.task_id} {e.title}</strong> <span className="muted small">· {e.responsible} · {new Date(e.at).toLocaleString("ru-RU")}</span>
              <div className="small">{e.text}</div>
            </div>
          ))}
        </>
      )}
    </div>
  );
}
