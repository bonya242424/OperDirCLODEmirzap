import { FormEvent, useCallback, useEffect, useState } from "react";
import { api, formatAgo, formatDate } from "../../api";
import type { User } from "../../types";
import { LEVELS } from "./AgentStructure";

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
  tools: string[];
  pending_actions: number;
  schedule_kind: "manual" | "interval" | "daily";
  interval_minutes: number | null;
  daily_time: string | null;
  enabled: boolean;
  level: "manager" | "analyst" | "ops";
  parent_id: number | null;
  next_run_at: string | null;
  last_run: Run | null;
  editable: boolean;
}
interface Preset {
  key: string;
  title: string;
  description: string;
  level: "manager" | "analyst" | "ops";
  instruction: string;
  tools: string[];
}
interface ToolInfo {
  key: string;
  title: string;
  description: string;
  kind: "read" | "write" | "confirm";
}
interface Action {
  id: number;
  agent: string;
  title: string;
  args: Record<string, unknown>;
}
interface Form {
  id: number | null;
  name: string;
  kind: string;
  instruction: string;
  tools: string[];
  schedule_kind: Agent["schedule_kind"];
  interval_minutes: string;
  daily_time: string;
  enabled: boolean;
  level: Agent["level"];
  parent_id: string;
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
  const [presets, setPresets] = useState<Preset[]>([]);
  const [tools, setTools] = useState<ToolInfo[]>([]);
  const [actions, setActions] = useState<Action[]>([]);
  const [form, setForm] = useState<Form | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [error, setError] = useState("");
  const fail = (e: unknown) => setError(e instanceof Error ? e.message : "Ошибка");

  const load = useCallback(() => {
    api<Agent[]>("/agents").then((a) => { setAgents(a); setError(""); }).catch(fail);
    api<Action[]>("/agents/actions").then(setActions).catch(() => {});
  }, []);
  useEffect(() => {
    load();
    api<Preset[]>("/agents/presets").then(setPresets).catch(() => {});
    api<ToolInfo[]>("/agents/tools").then(setTools).catch(() => {});
    const t = setInterval(load, 10_000);
    return () => clearInterval(t);
  }, [load]);
  useEffect(() => {
    if (open !== null) api<Run[]>(`/agents/${open}/runs`).then(setRuns).catch(() => {});
  }, [open, agents]);

  const blank = (): Form => ({ id: null, name: "", kind: "custom", instruction: "", tools: [], schedule_kind: "manual", interval_minutes: "60", daily_time: "09:00", enabled: true, level: "ops", parent_id: "" });

  async function save(e: FormEvent) {
    e.preventDefault();
    if (!form) return;
    const body = {
      name: form.name,
      kind: form.kind,
      instruction: form.instruction,
      tools: form.tools,
      schedule_kind: form.schedule_kind,
      interval_minutes: form.schedule_kind === "interval" ? Number(form.interval_minutes) : null,
      daily_time: form.schedule_kind === "daily" ? form.daily_time : null,
      enabled: form.enabled,
      level: form.level,
      parent_id: form.level === "manager" || !form.parent_id ? null : Number(form.parent_id),
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
      await api(`/agents/${a.id}`, { method: "PUT", body: { name: a.name, kind: a.kind, instruction: a.instruction, tools: a.tools, schedule_kind: a.schedule_kind, interval_minutes: a.interval_minutes, daily_time: a.daily_time, enabled: !a.enabled, level: a.level, parent_id: a.parent_id } });
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

  function applyPreset(key: string) {
    const p = presets.find((x) => x.key === key);
    if (!p || !form) return;
    setForm({ ...form, kind: p.key, level: p.level, parent_id: "", instruction: p.instruction, tools: p.tools, name: form.name || (p.key === "custom" ? "" : p.title) });
  }

  async function decide(x: Action, yes: boolean) {
    try {
      await api(`/agents/actions/${x.id}/${yes ? "approve" : "reject"}`, { method: "POST" });
      load();
    } catch (err) {
      fail(err);
    }
  }

  const TOOL_NOTE: Record<ToolInfo["kind"], string> = { read: "читает", write: "записывает данные", confirm: "только после вашего подтверждения" };

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
      {actions.length > 0 && (
        <div className="ag-actionsbox" role="region" aria-label="Ждут подтверждения">
          <h4>Агенты предлагают, решение за вами ({actions.length})</h4>
          {actions.map((x) => (
            <div key={x.id} className="ag-act">
              <div>
                <strong>{x.title}</strong>
                <p className="muted small">Предложил агент «{x.agent}»{typeof x.args.description === "string" && x.args.description ? `: ${x.args.description}` : ""}</p>
              </div>
              <div className="ag-actions">
                <button className="primary" onClick={() => decide(x, true)}>Подтвердить</button>
                <button onClick={() => decide(x, false)}>Отклонить</button>
              </div>
            </div>
          ))}
        </div>
      )}
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
                  {LEVELS[a.level].title} · {scheduleText(a)}{!a.enabled ? " · выключен" : ""}
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
                      <button onClick={() => setForm({ id: a.id, name: a.name, kind: a.kind, instruction: a.instruction, tools: a.tools, schedule_kind: a.schedule_kind, interval_minutes: String(a.interval_minutes ?? 60), daily_time: a.daily_time ?? "09:00", enabled: a.enabled, level: a.level, parent_id: a.parent_id ? String(a.parent_id) : "" })}>Изменить</button>
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
              Уровень в структуре
              <select value={form.level} onChange={(e) => setForm({ ...form, level: e.target.value as Agent["level"], parent_id: "" })}>
                {(Object.keys(LEVELS) as Agent["level"][]).map((l) => (
                  <option key={l} value={l}>{LEVELS[l].title}</option>
                ))}
              </select>
            </label>
            <p className="muted small">{LEVELS[form.level].role}</p>
            {form.level !== "manager" && (
              <label>
                Подчиняется
                <select value={form.parent_id} onChange={(e) => setForm({ ...form, parent_id: e.target.value })}>
                  <option value="">Никому</option>
                  {agents?.filter((x) => x.level === (form.level === "ops" ? "analyst" : "manager") && x.id !== form.id).map((x) => (
                    <option key={x.id} value={x.id}>{x.name}</option>
                  ))}
                </select>
              </label>
            )}
            {form.id === null && (
              <label>
                Начать с заготовки
                <select value={form.kind} onChange={(e) => applyPreset(e.target.value)}>
                  {presets.map((x) => (
                    <option key={x.key} value={x.key}>{x.title}</option>
                  ))}
                </select>
              </label>
            )}
            <textarea rows={5} required placeholder="Поручение своими словами: что делать, где смотреть, как показать результат" value={form.instruction} onChange={(e) => setForm({ ...form, instruction: e.target.value })} />
            <fieldset className="ag-tools">
              <legend>Что агенту можно делать</legend>
              {tools.map((t) => (
                <label key={t.key} className="ag-tool" title={t.description}>
                  <input type="checkbox" checked={form.tools.includes(t.key)} onChange={(e) => setForm({ ...form, tools: e.target.checked ? [...form.tools, t.key] : form.tools.filter((x) => x !== t.key) })} />
                  <span>{t.title} <small className={`muted tk-${t.kind}`}>{TOOL_NOTE[t.kind]}</small></span>
                </label>
              ))}
            </fieldset>
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
