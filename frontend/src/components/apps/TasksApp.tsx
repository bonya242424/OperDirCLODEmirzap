import { DragEvent, FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { api, formatDate } from "../../api";
import type { Board, BoardCard, User } from "../../types";

type Status = BoardCard["status"];
const PRIORITY: Record<BoardCard["priority"], string> = { low: "Низкий", normal: "Обычный", high: "Высокий" };

function initials(name: string): string {
  const parts = name.trim().split(/\s+/);
  return ((parts[0]?.[0] ?? "?") + (parts[1]?.[0] ?? "")).toUpperCase();
}

function deadlineChip(card: BoardCard): { text: string; tone: string } | null {
  if (!card.deadline) return null;
  const d = new Date(card.deadline);
  const days = Math.round((new Date(d.toDateString()).getTime() - new Date(new Date().toDateString()).getTime()) / 86400000);
  const date = d.toLocaleDateString("ru-RU", { day: "numeric", month: "short" });
  if (card.status === "done") return { text: date, tone: "" };
  if (card.overdue) return { text: `${date} · просрочено`, tone: "late" };
  if (days === 0) return { text: "сегодня", tone: "soon" };
  if (days === 1) return { text: "завтра", tone: "soon" };
  return { text: date, tone: days <= 3 ? "soon" : "" };
}

const toLocalInput = (iso: string | null): string => {
  if (!iso) return "";
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
};

interface Draft {
  id: number | null;
  title: string;
  description: string;
  status: Status;
  priority: BoardCard["priority"];
  assignee_id: string;
  deadline: string;
}

export default function TasksApp({ me }: { me: User }) {
  const [board, setBoard] = useState<Board | null>(null);
  const [users, setUsers] = useState<User[]>([]);
  const [error, setError] = useState("");
  const [search, setSearch] = useState("");
  const [who, setWho] = useState<"all" | "mine" | number>("all");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [quick, setQuick] = useState<{ status: Status; text: string } | null>(null);
  const original = draft && draft.id !== null ? board?.columns.flatMap((c) => c.cards).find((c) => c.id === `l-${draft.id}`) : undefined;
  const [dragId, setDragId] = useState<string | null>(null);
  const [over, setOver] = useState<{ status: Status; index: number } | null>(null);
  const [syncing, setSyncing] = useState(false);

  const fail = (e: unknown) => setError(e instanceof Error ? e.message : "Ошибка");

  const load = useCallback(() => {
    api<Board>("/board")
      .then((b) => {
        setBoard(b);
        setError("");
      })
      .catch(fail);
  }, []);

  useEffect(() => {
    load();
    api<User[]>("/users").then(setUsers).catch(() => {});
    const t = setInterval(load, 60_000);
    return () => clearInterval(t);
  }, [load]);

  const matches = useCallback(
    (c: BoardCard) => {
      if (search && !c.title.toLowerCase().includes(search.trim().toLowerCase())) return false;
      if (who === "mine") return c.assignee_id === me.id || (c.source === "bitrix" && c.assignee === (me.full_name || me.email));
      if (typeof who === "number") return c.assignee_id === who;
      return true;
    },
    [search, who, me],
  );

  const totals = useMemo(() => {
    const all = board?.columns.flatMap((c) => c.cards.filter(matches)) ?? [];
    return { open: all.filter((c) => c.status !== "done").length, late: all.filter((c) => c.overdue).length };
  }, [board, matches]);

  async function quickAdd(e: FormEvent) {
    e.preventDefault();
    if (!quick || !quick.text.trim()) return setQuick(null);
    try {
      await api("/board/tasks", { method: "POST", body: { title: quick.text, status: quick.status } });
      setQuick(null);
      load();
    } catch (err) {
      fail(err);
    }
  }

  function openCard(c: BoardCard) {
    if (!c.editable) return;
    setDraft({
      id: Number(c.id.split("-")[1]),
      title: c.title,
      description: c.description,
      status: c.status,
      priority: c.priority,
      assignee_id: c.assignee_id ? String(c.assignee_id) : "",
      deadline: toLocalInput(c.deadline),
    });
  }

  async function save(e: FormEvent) {
    e.preventDefault();
    if (!draft) return;
    const deadline = draft.deadline ? new Date(draft.deadline).toISOString() : null;
    try {
      if (draft.id === null) {
        await api("/board/tasks", {
          method: "POST",
          body: {
            title: draft.title,
            description: draft.description,
            status: draft.status,
            priority: draft.priority,
            assignee_id: draft.assignee_id ? Number(draft.assignee_id) : null,
            deadline,
          },
        });
      } else {
        await api(`/board/tasks/${draft.id}`, {
          method: "PATCH",
          body: {
            title: draft.title,
            description: draft.description,
            priority: draft.priority,
            assignee_id: draft.assignee_id ? Number(draft.assignee_id) : null,
            clear_assignee: !draft.assignee_id,
            deadline,
            clear_deadline: !deadline,
          },
        });
      }
      if (draft.id !== null && draft.status !== original?.status) {
        await api(`/board/tasks/${draft.id}/move`, { method: "POST", body: { status: draft.status, index: 0 } });
      }
      setDraft(null);
      load();
    } catch (err) {
      fail(err);
    }
  }

  async function remove() {
    if (!draft || draft.id === null || !window.confirm("Удалить задачу?")) return;
    try {
      await api(`/board/tasks/${draft.id}`, { method: "DELETE" });
      setDraft(null);
      load();
    } catch (err) {
      fail(err);
    }
  }

  async function sync() {
    setSyncing(true);
    try {
      await api("/tasks/sync", { method: "POST" });
      load();
    } catch (err) {
      fail(err);
    } finally {
      setSyncing(false);
    }
  }

  function overCard(e: DragEvent<HTMLElement>, status: Status, index: number) {
    e.preventDefault();
    e.stopPropagation();
    const rect = e.currentTarget.getBoundingClientRect();
    setOver({ status, index: e.clientY > rect.top + rect.height / 2 ? index + 1 : index });
  }

  async function drop(status: Status) {
    const id = dragId;
    const target = over && over.status === status ? over.index : 0;
    setDragId(null);
    setOver(null);
    if (!id || !board) return;
    const numeric = Number(id.split("-")[1]);
    // сразу показываем результат, сервер потом подтверждает порядок
    setBoard((b) => {
      if (!b) return b;
      let card: BoardCard | undefined;
      const cols = b.columns.map((c) => ({ ...c, cards: c.cards.filter((x) => (x.id === id ? ((card = x), false) : true)) }));
      if (!card) return b;
      const col = cols.find((c) => c.key === status)!;
      col.cards.splice(Math.min(target, col.cards.length), 0, { ...card, status });
      return { columns: cols };
    });
    try {
      await api(`/board/tasks/${numeric}/move`, { method: "POST", body: { status, index: target } });
    } catch (err) {
      fail(err);
    }
    load();
  }

  const people = users.filter((u) => u.is_active);

  return (
    <div className="kanban">
      <div className="kanban-bar">
        <div>
          <h2>Задачи</h2>
          <p className="muted">
            {totals.open} открыто{totals.late > 0 && <span className="late"> · {totals.late} просрочено</span>}
          </p>
        </div>
        <div className="kanban-tools">
          <input type="search" placeholder="Найти задачу…" value={search} onChange={(e) => setSearch(e.target.value)} />
          <select
            value={String(who)}
            onChange={(e) => setWho(e.target.value === "all" || e.target.value === "mine" ? e.target.value : Number(e.target.value))}
          >
            <option value="all">Все</option>
            <option value="mine">Мои</option>
            {people.map((u) => (
              <option key={u.id} value={u.id}>{u.full_name || u.email}</option>
            ))}
          </select>
          <button className="ghost" onClick={sync} disabled={syncing} title="Забрать свежие задачи из Битрикс24">
            {syncing ? "Обновляем…" : "↻ Битрикс24"}
          </button>
          <button
            className="primary"
            onClick={() => setDraft({ id: null, title: "", description: "", status: "new", priority: "normal", assignee_id: String(me.id), deadline: "" })}
          >
            + Новая задача
          </button>
        </div>
      </div>
      {error && <div className="error">{error}</div>}
      <div className="kanban-cols">
        {board?.columns.map((col) => {
          const cards = col.cards.filter(matches);
          return (
            <section
              key={col.key}
              className={`kcol${over?.status === col.key ? " drop" : ""}`}
              onDragOver={(e) => {
                e.preventDefault();
                setOver({ status: col.key, index: cards.length });
              }}
              onDrop={() => drop(col.key)}
            >
              <header>
                <span className={`kdot ${col.key}`} />
                <strong>{col.title}</strong>
                <span className="muted">{cards.length}</span>
                {cards.some((c) => c.overdue) && (
                  <span className="chipd late" title="Просроченные задачи в колонке">
                    {cards.filter((c) => c.overdue).length} просрочено
                  </span>
                )}
              </header>
              <div className="kcards">
                {cards.map((c, i) => {
                  const chip = deadlineChip(c);
                  return (
                    <article
                      key={c.id}
                      className={`kcard${dragId === c.id ? " dragging" : ""}${c.editable ? "" : " ro"}`}
                      draggable={c.editable}
                      onDragStart={() => setDragId(c.id)}
                      onDragEnd={() => {
                        setDragId(null);
                        setOver(null);
                      }}
                      onDragOver={(e) => overCard(e, col.key, i)}
                      onClick={() => openCard(c)}
                      tabIndex={0}
                      onKeyDown={(e) => e.key === "Enter" && openCard(c)}
                      title={c.editable ? "Открыть задачу" : "Задача из Битрикс24: менять её нужно там"}
                    >
                      {over?.status === col.key && over.index === i && dragId && <div className="kline" />}
                      <div className="ktitle">
                        {c.priority === "high" && <span className="prio" title="Высокий приоритет">!</span>}
                        {c.title}
                      </div>
                      <div className="kmeta">
                        {c.source === "bitrix" && <span className="tag">Б24</span>}
                        {chip && <span className={`chipd ${chip.tone}`} title={formatDate(c.deadline)}>{chip.text}</span>}
                        <span className="grow" />
                        {c.assignee && <span className="avatar" title={c.assignee}>{initials(c.assignee)}</span>}
                      </div>
                    </article>
                  );
                })}
                {over?.status === col.key && over.index >= cards.length && dragId && <div className="kline" />}
                {cards.length === 0 && quick?.status !== col.key && (
                  <p className="kempty">{col.key === "done" ? "Здесь появятся закрытые задачи" : "Перетащите сюда задачу"}</p>
                )}
                {quick?.status === col.key ? (
                  <form onSubmit={quickAdd} className="kquick">
                    <input
                      autoFocus
                      placeholder="Название задачи, Enter"
                      value={quick.text}
                      onChange={(e) => setQuick({ ...quick, text: e.target.value })}
                      onBlur={() => !quick.text.trim() && setQuick(null)}
                      onKeyDown={(e) => e.key === "Escape" && setQuick(null)}
                    />
                  </form>
                ) : (
                  <button className="kadd" onClick={() => setQuick({ status: col.key, text: "" })}>+ Добавить</button>
                )}
              </div>
            </section>
          );
        })}
      </div>

      {draft && (
        <div className="modal-bg" onPointerDown={() => setDraft(null)}>
          <form className="modal" onSubmit={save} onPointerDown={(e) => e.stopPropagation()}>
            <h3>{draft.id === null ? "Новая задача" : "Задача"}</h3>
            <input autoFocus required placeholder="Что нужно сделать" value={draft.title} onChange={(e) => setDraft({ ...draft, title: e.target.value })} />
            <textarea rows={4} placeholder="Описание" value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })} />
            <div className="modal-grid">
              <label>
                Исполнитель
                <select value={draft.assignee_id} onChange={(e) => setDraft({ ...draft, assignee_id: e.target.value })}>
                  <option value="">Не назначен</option>
                  {people.map((u) => (
                    <option key={u.id} value={u.id}>{u.full_name || u.email}</option>
                  ))}
                </select>
              </label>
              <label>
                Срок
                <input type="datetime-local" value={draft.deadline} onChange={(e) => setDraft({ ...draft, deadline: e.target.value })} />
              </label>
              <label>
                Приоритет
                <select value={draft.priority} onChange={(e) => setDraft({ ...draft, priority: e.target.value as BoardCard["priority"] })}>
                  {Object.entries(PRIORITY).map(([k, v]) => (
                    <option key={k} value={k}>{v}</option>
                  ))}
                </select>
              </label>
              <label>
                Колонка
                <select value={draft.status} onChange={(e) => setDraft({ ...draft, status: e.target.value as Status })}>
                  {board?.columns.map((c) => (
                    <option key={c.key} value={c.key}>{c.title}</option>
                  ))}
                </select>
              </label>
            </div>
            <div className="modal-actions">
              {draft.id !== null && <button type="button" className="ghost danger" onClick={remove}>Удалить</button>}
              <span className="grow" />
              <button type="button" onClick={() => setDraft(null)}>Отмена</button>
              <button type="submit" className="primary">Сохранить</button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}
