import { useCallback, useEffect, useState } from "react";
import { api } from "../../api";

interface Run {
  id: number;
  status: "running" | "done" | "failed" | "stopped";
  summary: string;
  saved: { supplier: string; file_id: number; name: string }[];
  log?: string[];
}
interface Targets {
  rdp: { id: number; title: string; host: string }[];
}

const STATUS = { running: "Идёт…", done: "Готово", failed: "Не получилось", stopped: "Остановлено" } as const;

/** Запуск агента, который собирает документы «Сборка» из 1С через терминал и сохраняет их в «Файлы». */
export default function AssemblyRunBox({ from, to }: { from: string; to: string }) {
  const [targets, setTargets] = useState<Targets | null>(null);
  const [conn, setConn] = useState(0);
  const [note, setNote] = useState("");
  const [run, setRun] = useState<Run | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api<Targets>("/remote/targets")
      .then((t) => {
        setTargets(t);
        if (t.rdp[0]) setConn(t.rdp[0].id);
      })
      .catch(() => setTargets({ rdp: [] }));
    api<Run[]>("/assembly/runs")
      .then((r) => r[0] && r[0].status === "running" && setRun(r[0]))
      .catch(() => undefined);
  }, []);

  const poll = useCallback(() => {
    if (!run) return;
    api<Run>(`/assembly/runs/${run.id}`).then(setRun).catch(() => undefined);
  }, [run]);
  useEffect(() => {
    if (!run || run.status !== "running") return;
    const t = window.setInterval(poll, 2500);
    return () => window.clearInterval(t);
  }, [run, poll]);

  async function start() {
    setError("");
    try {
      setRun(await api<Run>("/assembly/run", { method: "POST", body: { connection_id: conn, date_from: from, date_to: to, note } }));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Ошибка");
    }
  }

  async function stop() {
    if (!run) return;
    setRun(await api<Run>(`/assembly/runs/${run.id}/stop`, { method: "POST" }));
  }

  if (!targets) return null;
  const running = run?.status === "running";
  return (
    <div className="ag-problem assembly-run">
      <strong>Собрать из 1С за период {new Date(from).toLocaleDateString("ru-RU")}–{new Date(to).toLocaleDateString("ru-RU")}</strong>
      <p className="muted small">
        Агент откроет в 1С документы «Сборка» за этот период, скопирует таблицы и сохранит в «Файлы»: отдельная папка на каждого поставщика, в названии файла дата сохранения.
        Он только читает: проведение, запись и удаление в 1С заблокированы. Действует по инструкциям из «Инструкции 1С». Следить можно в «Удалённых окнах».
      </p>
      {targets.rdp.length === 0 ? (
        <p className="error">Сначала добавьте терминал: «Интеграции → Подключения → Терминал (RDP)».</p>
      ) : (
        <div className="row">
          <select value={conn} onChange={(e) => setConn(Number(e.target.value))} disabled={running}>
            {targets.rdp.map((t) => <option key={t.id} value={t.id}>{t.title} · {t.host}</option>)}
          </select>
          <input placeholder="Пожелание (необязательно)" value={note} onChange={(e) => setNote(e.target.value)} disabled={running} />
          {running ? <button className="ghost danger" onClick={stop}>Остановить</button> : <button className="primary" onClick={start}>Собрать из 1С</button>}
        </div>
      )}
      {error && <p className="error">{error}</p>}
      {run && (
        <div>
          <p><strong>{STATUS[run.status]}</strong> {run.summary}</p>
          {run.saved.length > 0 && (
            <ul>{run.saved.map((s) => <li key={s.file_id}>{s.supplier}: {s.name}</li>)}</ul>
          )}
          {run.log && run.log.length > 0 && <pre className="remote-log">{run.log.slice(-25).join("\n")}</pre>}
        </div>
      )}
    </div>
  );
}
