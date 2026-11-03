import { useCallback, useEffect, useRef, useState } from "react";
import { api, getToken } from "../../api";

interface Run {
  id: number;
  connection_id?: number;
  status: "running" | "done" | "failed" | "stopped";
  summary: string;
  saved: { supplier: string; file_id: number; name: string; duplicate?: boolean }[];
  log?: string[];
  note?: string;
  created_at?: string;
  qa?: { id: number; question: string; answer: string | null }[];
  question?: { id: number; question: string; answer: string | null } | null;
}
interface Targets {
  rdp: { id: number; title: string; host: string }[];
}

const STATUS = { running: "Идёт…", done: "Готово", failed: "Не получилось", stopped: "Остановлено" } as const;

/** Живой экран терминала во время сборки: видно, куда агент кликает и что открыто. Красная точка — последний клик. */
function LiveScreen({ conn, running, log }: { conn: number; running: boolean; log: string[] }) {
  const [src, setSrc] = useState("");
  const prev = useRef("");
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const res = await fetch(`/api/remote/rdp/${conn}/screen`, { headers: { Authorization: `Bearer ${getToken() ?? ""}` } });
        if (!res.ok || !alive) return;
        const u = URL.createObjectURL(await res.blob());
        if (prev.current) URL.revokeObjectURL(prev.current);
        prev.current = u;
        setSrc(u);
      } catch {
        /* следующий кадр */
      }
    };
    tick();
    const t = running ? window.setInterval(tick, 1500) : undefined;
    return () => {
      alive = false;
      if (t) window.clearInterval(t);
    };
  }, [conn, running]);
  const last = [...log].reverse().map((l) => l.match(/\((\d+), (\d+)\)/)).find(Boolean);
  if (!src) return null;
  return (
    <div className="assembly-live">
      <img src={src} alt="Экран терминала" />
      {last && <i style={{ left: `${(Number(last[1]) / 1280) * 100}%`, top: `${(Number(last[2]) / 800) * 100}%` }} />}
    </div>
  );
}

/** Запуск агента, который собирает документы «Сборка» из 1С через терминал и сохраняет их в «Файлы». */
export default function AssemblyRunBox({ from, to }: { from: string; to: string }) {
  const [targets, setTargets] = useState<Targets | null>(null);
  const [conn, setConn] = useState(0);
  const [note, setNote] = useState("");
  const [run, setRun] = useState<Run | null>(null);
  const [error, setError] = useState("");
  const [history, setHistory] = useState<Run[]>([]);
  const [reply, setReply] = useState("");

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

  const loadHistory = useCallback(() => {
    api<Run[]>("/assembly/runs").then(setHistory).catch(() => undefined);
  }, []);
  useEffect(() => {
    loadHistory();
  }, [loadHistory, run?.status]);

  async function answer(id: number) {
    if (!reply.trim()) return;
    try {
      await api(`/assembly/questions/${id}/answer`, { method: "POST", body: { answer: reply } });
      setReply("");
      if (run) setRun(await api<Run>(`/assembly/runs/${run.id}`));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Ошибка");
    }
  }

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
        Он только читает: проведение, запись и удаление в 1С заблокированы. Действует по инструкциям из «Инструкции 1С». Экран терминала виден ниже во время работы.
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
          {run.question && (
            <div className="assembly-ask">
              <strong>Агент спрашивает:</strong>
              <p>{run.question.question}</p>
              <div className="row">
                <input placeholder="Ваш ответ" value={reply} onChange={(e) => setReply(e.target.value)} onKeyDown={(e) => e.key === "Enter" && answer(run.question!.id)} />
                <button className="primary" onClick={() => answer(run.question!.id)}>Ответить</button>
              </div>
              <p className="muted small">Ответ сохранится в «Инструкции 1С», и в следующий раз агент не будет спрашивать то же самое.</p>
            </div>
          )}
          {run.saved.length > 0 && (
            <ul>{run.saved.map((s) => <li key={s.file_id + (s.duplicate ? "d" : "")}>{s.supplier}: {s.name}{s.duplicate ? " — дубль, повторно не сохранён" : ""}</li>)}</ul>
          )}
          <LiveScreen conn={run.connection_id ?? conn} running={running} log={run.log ?? []} />
          {run.log && run.log.length > 0 && <pre className="remote-log">{run.log.slice(-40).join("\n")}</pre>}
        </div>
      )}
      {history.length > 0 && (
        <details className="assembly-history">
          <summary>История запусков ({history.length})</summary>
          {history.map((h) => (
            <div key={h.id} className="assembly-history-item">
              <strong>#{h.id} · {STATUS[h.status]}</strong>
              {h.created_at && <span className="muted small"> · {new Date(h.created_at).toLocaleString("ru-RU")}</span>}
              {h.note && <div className="small">Запрос: {h.note}</div>}
              {h.summary && <div className="small muted">{h.summary}</div>}
              {(h.qa ?? []).map((q) => (
                <div key={q.id} className="small">В: {q.question}<br />О: {q.answer ?? "ответа нет"}</div>
              ))}
            </div>
          ))}
        </details>
      )}
    </div>
  );
}
