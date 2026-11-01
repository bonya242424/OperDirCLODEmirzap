import { useEffect, useState } from "react";
import { api } from "../../api";
import type { Board, Metric } from "../../types";

export default function FocusWidget({ onOpen }: { onOpen: (key: string) => void }) {
  const [board, setBoard] = useState<Board | null>(null);
  const [metrics, setMetrics] = useState<Metric[] | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () => {
      api<Board>("/board").then((b) => alive && setBoard(b)).catch(() => {});
      api<Metric[]>("/metrics").then((m) => alive && setMetrics(m)).catch(() => {});
    };
    load();
    const t = setInterval(load, 60_000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  const cards = board?.columns.flatMap((c) => c.cards).filter((c) => c.status !== "done") ?? [];
  const late = cards.filter((c) => c.overdue);
  const bad = metrics?.filter((m) => m.status === "bad" || m.status === "warning") ?? [];
  const empty = board && metrics && late.length === 0 && bad.length === 0;

  return (
    <div className="widget">
      <div className="widget-title">Требует внимания</div>
      {!board || !metrics ? (
        <p className="muted">Загрузка…</p>
      ) : empty ? (
        <p className="muted">
          {cards.length === 0 && metrics.length === 0
            ? "Пока нет ни задач, ни показателей. Добавьте их в разделах «Задачи» и «Директоры»."
            : "Просроченных задач и проблемных показателей нет."}
        </p>
      ) : (
        <ul className="widget-list">
          {late.slice(0, 3).map((c) => (
            <li key={c.id}>
              <button className="linkish" onClick={() => onOpen("tasks")}>
                <span className="late">Просрочено</span> {c.title}
              </button>
              <span className="muted">{c.assignee || "не назначен"}</span>
            </li>
          ))}
          {late.length > 3 && <li className="muted">и ещё {late.length - 3} просрочено</li>}
          {bad.slice(0, 3).map((m) => (
            <li key={m.id}>
              <button className="linkish" onClick={() => onOpen(m.direction)}>
                <span className={m.status === "bad" ? "late" : "warnc"}>{m.status === "bad" ? "Плохо" : "Внимание"}</span> {m.name}
              </button>
              <span className="muted">
                {m.latest} {m.unit}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
