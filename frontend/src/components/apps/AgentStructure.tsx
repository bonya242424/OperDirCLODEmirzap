import { useEffect, useState } from "react";
import { api, formatAgo } from "../../api";

export interface StructAgent {
  id: number;
  name: string;
  kind_title: string;
  level: "manager" | "analyst" | "ops";
  parent_id: number | null;
  enabled: boolean;
  last_run: { status: string; started_at: string; finished_at: string | null } | null;
}

export const LEVELS: Record<StructAgent["level"], { title: string; role: string }> = {
  manager: { title: "Руководитель", role: "Ставит задачи через промт и принимает выводы аналитиков" },
  analyst: { title: "Аналитик", role: "Анализирует отчёты опер-штаба, контролирует и делает выводы" },
  ops: { title: "Опер-штаб", role: "По расписанию выполняет действия и собирает показатели" },
};
const STATUS: Record<string, string> = { queued: "в очереди", running: "работает", done: "готово", failed: "ошибка", stopped: "остановлен" };

function Node({ a, byParent }: { a: StructAgent; byParent: Map<number, StructAgent[]> }) {
  const kids = byParent.get(a.id) ?? [];
  return (
    <li>
      <div className={`st-node lv-${a.level}${a.enabled ? "" : " off"}`}>
        <strong>{a.name}</strong>
        <span className="muted small">{a.kind_title}</span>
        <span className={`chipd st-${a.last_run?.status ?? "none"}`}>
          {a.last_run ? `${STATUS[a.last_run.status] ?? a.last_run.status} · ${formatAgo(a.last_run.finished_at ?? a.last_run.started_at)}` : "не запускался"}
        </span>
      </div>
      {kids.length > 0 && (
        <ul>
          {kids.map((k) => (
            <Node key={k.id} a={k} byParent={byParent} />
          ))}
        </ul>
      )}
    </li>
  );
}

export default function AgentStructure() {
  const [agents, setAgents] = useState<StructAgent[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    const load = () => api<StructAgent[]>("/agents").then(setAgents).catch((e) => setError(e instanceof Error ? e.message : "Ошибка"));
    load();
    const t = setInterval(load, 15_000);
    return () => clearInterval(t);
  }, []);

  const ids = new Set(agents?.map((a) => a.id));
  const byParent = new Map<number, StructAgent[]>();
  const roots: StructAgent[] = [];
  for (const a of agents ?? []) {
    if (a.parent_id !== null && ids.has(a.parent_id)) byParent.set(a.parent_id, [...(byParent.get(a.parent_id) ?? []), a]);
    else roots.push(a);
  }
  const top = roots.filter((a) => a.level === "manager");
  const loose = roots.filter((a) => a.level !== "manager");

  return (
    <div className="ag-struct">
      <div className="st-levels">
        {(Object.keys(LEVELS) as StructAgent["level"][]).map((k, i) => (
          <div key={k} className={`st-level lv-${k}`}>
            <b>{i + 1}. {LEVELS[k].title}</b>
            <span className="muted small">{LEVELS[k].role}</span>
          </div>
        ))}
      </div>
      {error && <div className="error">{error}</div>}
      {agents && agents.length === 0 && (
        <div className="ag-hint">Структура пуста. Создайте руководителя, аналитика и агентов опер-штаба во вкладке «Агенты» и укажите, кто кому подчиняется.</div>
      )}
      {top.length > 0 && (
        <ul className="st-tree">
          {top.map((a) => (
            <Node key={a.id} a={a} byParent={byParent} />
          ))}
        </ul>
      )}
      {loose.length > 0 && (
        <>
          <h4>Без руководителя</h4>
          <p className="muted small">Эти агенты работают сами по себе. Чтобы включить их в цепочку, откройте агента и выберите, кому он подчиняется.</p>
          <ul className="st-tree">
            {loose.map((a) => (
              <Node key={a.id} a={a} byParent={byParent} />
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
