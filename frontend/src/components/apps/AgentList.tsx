import { FormEvent, useCallback, useEffect, useState } from "react";
import { api, formatAgo, formatDate } from "../../api";
import type { User } from "../../types";

interface Run {
  id: number;
  trigger: string;
  status: "queued" | "running" | "done" | "failed" | "stopped";
  summary: string;
  log: { at: string; text: string }[];
  started_at: string;
  finished_at: string | null;
}
interface Agent {
  id: number;
  owner: string;
  name: string;
  kind: string;
  kind_title: string;
  instruction: string;
  schedule_kind: "manual" | "interval" | "daily";
  interval_minutes: number | null;
  daily_time: string | null;
  enabled: boolean;
  next_run_at: string | null;
  last_run: Run | null;
  editable: boolean;
}
interface Kind {
  key: string;
  title: string;
  description: string;
  needs_instruction: boolean;
}
interface Form {
  id: number | null;
  name: string;
  kind: string;
  instruction: string;
  schedule_kind: Agent["schedule_kind"];
  interval_minutes: string;
  daily_time: string;
  enabled: boolean;
}

const STATUS: Record<Run["status"], string> = { queued: "в очереди", running: "работает", done: "готово", failed: "ошибка", stopped: "остановлен" };
const INTERVALS: [number, string][] = [[15, "каждые 15 минут"], [60, "каждый час"], [180, "каждые 3 часа"], [720, "каждые 12 часов"], [1440, "каждый день"]];

function scheduleText(a: Agent): string {
  if (a.schedule_kind === "daily") return `каждый день в ${a.daily_time} (МСК)`;
  if (a.schedule_kind === "interval") return INTERVALS.find(([m]) => m === a.interval_minutes)?.[1] ?? `каждые ${a.interval_minutes} мин`;
  return "только вручную";
}

export default function AgentList({ me }: { me: User }) {
  const [agents, setAgents] = useState<Agent[] | null>(null);
  const [kinds, setKinds] = useState<Kind[]>([]);
  const [form, setForm] = useState<Form | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [error, setError] = useState("");
  const fail = (e: unknown) => setError(e instanceof Error ? e.message : "Ошибка");

  const load = useCallback(() => {
    api<Agent[]>("/agents").then((a) => { setAgents(a); setError(""); }).catch(fail);
  }, []);
  useEffect(() => {
    load();
    api<Kind[]>("/agents/kinds").then(setKinds).catch(() => {});
    const t = setInterval(load, 10_000);
    return () => clearInterval(t);
  }, [load]);
  useEffect(() => {
    if (open !== null) api<Run[]>(`/agents/${open}/runs`).then(setRuns).catch(() => {});
  }, [open, agents]);

  const kind = (key: string) => kinds.find((k) => k.key === key);
  const blank = (): Form => ({ id: null, name: "", kind: kinds[0]?.key ?? "onec_files", instruction: "", schedule_kind: "manual", interval_minutes: "60", daily_time: "09:00", enabled: true });

  async function save(e: FormEvent) {
    e.preventDefault();
    if (!form) return;
    const body = {
      name: form.name,
      kind: form.kind,
      instruction: form.instruction,
      schedule_kind: form.schedule_kind,
      interval_minutes: form.schedule_kind === "interval" ? Number(form.interval_minutes) : null,
      daily_time: form.schedule_kind === "daily" ? form.daily_time : null,
      enabled: form.enabled,
    };
    try {
      if (form.id === null) await api("/agents", { method: "POST", body });
      else await api(`/agents/${form.id}`, { method: "PUT", body });
      setForm(null);
      load();
    } catch (err) {
      fail(err);
    }
  }

  async function run(a: Agent) {
    try {
      await api(`/agents/${a.id}/run`, { method: "POST" });
      setOpen(a.id);
      load();
    } catch (err) {
      fail(err);
    }
  }

  async function toggle(a: Agent) {
    try {
      await api(`/agents/${a.id}`, { method: "PUT", body: { name: a.name, kind: a.kind, instruction: a.instruction, schedule_kind: a.schedule_kind, interval_minutes: a.interval_minutes, daily_time: a.daily_time, enabled: !a.enabled } });
      load();
    } catch (err) {
      fail(err);
    }
  }

  async function remove(a: Agent) {
    if (!window.confirm(`Удалить агента «${a.name}» вместе с историей запусков?`)) return;
    try {
      await api(`/agents/${a.id}`, { method: "DELETE" });
      setOpen(null);
      load();
    } catch (err) {
      fail(err);
    }
  }

  const k = form ? kind(form.kind) : undefined;

  return (
    <div className="ag-list">
      <div className="app-head">
        <div>
          <h2>{me.role === "owner" ? "Все агенты" : "Мои агенты"}</h2>
          <p className="muted small">Агент работает с вашими доступами и сам ничего необратимого не отправляет. Запуски идут вручную или по расписанию.</p>
        </div>
        <button className="primary" onClick={() => setForm(blank())}>+ Новый агент</button>
      </div>
      {error && <div className="error">{error}</div>}
      {agents && agents.length === 0 && (
        <div className="ag-hint">Агентов пока нет. Создайте первого, например «Выгрузки из 1С» каждый день в 09:00: он проверит, пришли ли новые файлы, и запишет итог в журнал.</div>
      )}
      <ul className="ag-cards">
        {agents?.map((a) => (
          <li key={a.id} className={`ag-card${a.enabled ? "" : " off"}`}>
            <div className="ag-card-top" onClick={() => setOpen(open === a.id ? null : a.id)}>
              <div>
                <strong>{a.name}</strong>
                <p className="muted small">
                  {a.kind_title} · {scheduleText(a)}{!a.enabled ? " · выключен" : ""}
                  {me.role === "owner" ? ` · ${a.owner}` : ""}
                </p>
              </div>
              <div className="ag-card-state">
                {a.last_run ? (
                  <span className={`chipd st-${a.last_run.status}`}>{STATUS[a.last_run.status]} · {formatAgo(a.last_run.finished_at ?? a.last_run.started_at)}</span>
                ) : (
                  <span className="chipd">не запускался</span>
                )}
                {a.next_run_at && a.enabled && <span className="muted small">следующий: {formatDate(a.next_run_at)}</span>}
              </div>
            </div>
            {open === a.id && (
              <div className="ag-card-body">
                {a.instruction && <p className="tdesc">{a.instruction}</p>}
                <div className="ag-actions">
                  <button className="primary" onClick={() => run(a)} disabled={!a.editable || a.last_run?.status === "running" || a.last_run?.status === "queued"}>Запустить сейчас</button>
                  {a.editable && (
                    <>
                      <button onClick={() => setForm({ id: a.id, name: a.name, kind: a.kind, instruction: a.instruction, schedule_kind: a.schedule_kind, interval_minutes: String(a.interval_minutes ?? 60), daily_time: a.daily_time ?? "09:00", enabled: a.enabled })}>Изменить</button>
                      <button onClick={() => toggle(a)}>{a.enabled ? "Выключить" : "Включить"}</button>
                      <button className="ghost danger" onClick={() => remove(a)}>Удалить</button>
                    </>
                  )}
                </div>
                <h4>История запусков</h4>
                {runs.length === 0 ? (
                  <p className="muted small">Запусков ещё не было.</p>
                ) : (
                  <ul className="ag-runs">
                    {runs.map((r) => (
                      <li key={r.id}>
                        <div>
                          <span className={`chipd st-${r.status}`}>{STATUS[r.status]}</span>{" "}
                          <span className="muted small">{formatDate(r.started_at)} · {r.trigger === "schedule" ? "по расписанию" : "вручную"}</span>
                        </div>
                        {r.summary && <p className="ag-sumtext">{r.summary}</p>}
                        {r.log.length > 0 && (
                          <details>
                            <summary className="muted small">Журнал ({r.log.length})</summary>
                            {r.log.map((l, i) => (
                              <p key={i} className="small">{new Date(l.at).toLocaleTimeString("ru-RU")} {l.text}</p>
                            ))}
                          </details>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}
          </li>
        ))}
      </ul>

      {form && (
        <div className="modal-bg" onPointerDown={() => setForm(null)}>
          <form className="modal" onSubmit={save} onPointerDown={(e) => e.stopPropagation()}>
            <h3>{form.id === null ? "Новый агент" : "Агент"}</h3>
            <input autoFocus required placeholder="Название, например «Выгрузки 1С по утрам»" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
            <label>
              Что делает
              <select value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value })} disabled={form.id !== null}>
                {kinds.map((x) => (
                  <option key={x.key} value={x.key}>{x.title}</option>
                ))}
              </select>
            </label>
            {k && <p className="muted small">{k.description}</p>}
            {k?.needs_instruction && (
              <textarea rows={4} placeholder="Поручение: что нужно сделать и в каком виде показать результат" value={form.instruction} onChange={(e) => setForm({ ...form, instruction: e.target.value })} />
            )}
            <label>
              Когда запускать
              <select value={form.schedule_kind} onChange={(e) => setForm({ ...form, schedule_kind: e.target.value as Form["schedule_kind"] })}>
                <option value="manual">Только вручную</option>
                <option value="interval">Через равные промежутки</option>
                <option value="daily">Каждый день в одно время</option>
              </select>
            </label>
            {form.schedule_kind === "interval" && (
              <select value={form.interval_minutes} onChange={(e) => setForm({ ...form, interval_minutes: e.target.value })}>
                {INTERVALS.map(([m, label]) => (
                  <option key={m} value={m}>{label}</option>
                ))}
              </select>
            )}
            {form.schedule_kind === "daily" && (
              <label>
                Время по Москве
                <input type="time" required value={form.daily_time} onChange={(e) => setForm({ ...form, daily_time: e.target.value })} />
              </label>
            )}
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
