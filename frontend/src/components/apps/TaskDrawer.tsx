import { FormEvent, useEffect, useRef, useState } from "react";
import { api, formatDate } from "../../api";
import type { BoardCard } from "../../types";

interface Related {
  id: string;
  title: string;
  relation: "parent" | "child" | "same_group";
  status: string;
  assignee: string;
}
interface Msg {
  id: number;
  role: "user" | "assistant";
  content: string;
}

const REL: Record<Related["relation"], string> = { parent: "Родительская", child: "Подзадача", same_group: "Из того же проекта" };
const STATUS: Record<string, string> = { new: "Новая", in_progress: "В работе", review: "На проверке", done: "Готово" };

export default function TaskDrawer({
  card,
  onClose,
  onEdit,
  onOpenRelated,
}: {
  card: BoardCard;
  onClose: () => void;
  onEdit: () => void;
  onOpenRelated: (id: string) => void;
}) {
  const [related, setRelated] = useState<Related[]>([]);
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const end = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setRelated([]);
    setMsgs([]);
    setError("");
    api<Related[]>(`/board/cards/${card.id}/related`).then(setRelated).catch(() => {});
    api<Msg[]>(`/board/cards/${card.id}/chat`).then(setMsgs).catch(() => {});
  }, [card.id]);

  useEffect(() => {
    end.current?.scrollIntoView({ block: "end" });
  }, [msgs, busy]);

  async function send(e: FormEvent) {
    e.preventDefault();
    const message = text.trim();
    if (!message || busy) return;
    setBusy(true);
    setError("");
    setMsgs((m) => [...m, { id: -Date.now(), role: "user", content: message }]);
    setText("");
    try {
      const r = await api<{ reply: Msg }>(`/board/cards/${card.id}/chat`, { method: "POST", body: { message } });
      setMsgs((m) => [...m, r.reply]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Ошибка");
      setMsgs((m) => m.slice(0, -1));
      setText(message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <aside className="tdrawer" aria-label="Задача">
      <header>
        <div>
          <span className={`chipd${card.overdue ? " late" : ""}`}>{STATUS[card.status]}{card.overdue ? " · просрочена" : ""}</span>
          <h3>{card.title}</h3>
          <p className="muted">
            {card.assignee || "Не назначен"} · срок {card.deadline ? formatDate(card.deadline) : "не задан"}
            {card.source === "bitrix" ? " · Битрикс24" : ""}
          </p>
        </div>
        <button className="ghost" onClick={onClose} aria-label="Закрыть">✕</button>
      </header>
      <div className="tdrawer-body">
        {card.description && <p className="tdesc">{card.description}</p>}
        {card.editable && <button onClick={onEdit}>Изменить задачу</button>}
        <h4>Связанные задачи</h4>
        {related.length === 0 ? (
          <p className="muted small">Связей нет: у задачи нет родителя, подзадач и соседей по проекту.</p>
        ) : (
          <ul className="trel">
            {related.map((r) => (
              <li key={r.id}>
                <button onClick={() => onOpenRelated(r.id)}>
                  <span className="muted small">{REL[r.relation]}</span>
                  <span>{r.title}</span>
                  <span className="muted small">{r.assignee} · {STATUS[r.status] ?? r.status}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
        <h4>Чат с агентом по этой задаче</h4>
        <div className="tchat">
          {msgs.length === 0 && <p className="muted small">Спросите, с чего начать, или попросите составить текст. Агент видит эту задачу и связанные.</p>}
          {msgs.map((m) => (
            <div key={m.id} className={`tmsg ${m.role}`}>{m.content}</div>
          ))}
          {busy && <div className="tmsg assistant muted">Думаю…</div>}
          <div ref={end} />
        </div>
        {error && <p className="error">{error}</p>}
      </div>
      <form className="tsend" onSubmit={send}>
        <input placeholder="Сообщение агенту" value={text} onChange={(e) => setText(e.target.value)} />
        <button className="primary" disabled={busy || !text.trim()}>Отправить</button>
      </form>
    </aside>
  );
}
