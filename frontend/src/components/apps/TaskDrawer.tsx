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
interface Detail {
  status_name: string;
  created_at: string | null;
  changed_at: string | null;
  closed_at: string | null;
  comments_count: number;
  comments: { id: number; author: string; text: string; at: string }[];
  comments_error: string;
}
const STATUS: Record<string, string> = { new: "План", in_progress: "В работе", review: "В работе", paused: "На паузе", cancelled: "Отмена", done: "Завершено" };

export default function TaskDrawer({
  card,
  onChanged,
  onClose,
  onEdit,
  onOpenRelated,
}: {
  card: BoardCard;
  onChanged?: () => void;
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
  const [detail, setDetail] = useState<Detail | null>(null);
  const [processes, setProcesses] = useState<string[]>([]);

  useEffect(() => {
    setDetail(null);
    if (card.source === "bitrix") api<Detail>(`/board/cards/${card.id}/detail`).then(setDetail).catch(() => {});
  }, [card.id, card.source]);
  useEffect(() => {
    api<string[]>("/board/processes").then(setProcesses).catch(() => {});
  }, []);

  async function saveTags(body: { process: string }) {
    try {
      await api(`/board/cards/${card.id}/tags`, { method: "PATCH", body });
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Ошибка");
    }
  }

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
        {card.source === "bitrix" && (
          <>
            <div className="tdtags">
              <label>
                Процесс {card.tag_source === "ai" && <span className="muted small">(оценка ИИ, можно поправить)</span>}
                <select value={card.process} onChange={(e) => saveTags({ process: e.target.value })}>
                  <option value="">Не определён</option>
                  {processes.map((p) => (
                    <option key={p} value={p}>{p}</option>
                  ))}
                </select>
              </label>
            </div>
            {detail && (
              <p className="muted small">
                Создана {detail.created_at ? formatDate(detail.created_at) : "—"} · изменена {detail.changed_at ? formatDate(detail.changed_at) : "—"}
                {detail.closed_at ? ` · закрыта ${formatDate(detail.closed_at)}` : ""}
                {detail.status_name ? ` · в Б24: ${detail.status_name}` : ""}
              </p>
            )}
            <h4>Комментарии{detail ? ` (${detail.comments_count})` : ""}</h4>
            {!detail ? <p className="muted small">Загружаем…</p> : detail.comments_error ? <p className="error">{detail.comments_error}</p> : detail.comments.length === 0 ? (
              <p className="muted small">Комментариев нет.</p>
            ) : (
              detail.comments.map((c) => (
                <div key={c.id} className="tcomm"><strong>{c.author}</strong> <span className="muted small">{c.at ? formatDate(c.at) : ""}</span><br />{c.text}</div>
              ))
            )}
          </>
        )}
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
