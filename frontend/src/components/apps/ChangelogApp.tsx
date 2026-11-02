import { useEffect, useState } from "react";
import { api } from "../../api";

interface Entry {
  id: number;
  kind: "added" | "improved" | "removed" | "fixed";
  kind_label: string;
  title: string;
  text: string;
  released_on: string;
}

const day = (iso: string): string => new Date(iso).toLocaleDateString("ru-RU", { day: "numeric", month: "long", year: "numeric" });

export default function ChangelogApp() {
  const [items, setItems] = useState<Entry[] | null>(null);
  const [kind, setKind] = useState<"all" | Entry["kind"]>("all");
  const [error, setError] = useState("");

  useEffect(() => {
    api<Entry[]>("/changelog").then(setItems).catch((e) => setError(e instanceof Error ? e.message : "Ошибка"));
  }, []);

  if (error) return <div className="placeholder"><p className="error">{error}</p></div>;
  if (!items) return <div className="placeholder"><p className="muted">Загрузка…</p></div>;

  const shown = items.filter((i) => kind === "all" || i.kind === kind);
  const groups = shown.reduce<Record<string, Entry[]>>((acc, i) => ((acc[i.released_on] ||= []).push(i), acc), {});

  return (
    <div className="changelog">
      <div className="kanban-bar">
        <h2>Журнал изменений</h2>
        <select value={kind} onChange={(e) => setKind(e.target.value as typeof kind)} aria-label="Фильтр по виду">
          <option value="all">Все изменения</option>
          <option value="added">Добавили</option>
          <option value="improved">Доработали</option>
          <option value="removed">Убрали</option>
          <option value="fixed">Исправили</option>
        </select>
      </div>
      {shown.length === 0 && <p className="muted">Записей с таким видом пока нет.</p>}
      {Object.entries(groups).map(([d, list]) => (
        <section key={d}>
          <h4>{day(d)}</h4>
          <ul>
            {list.map((e) => (
              <li key={e.id}>
                <span className={`chip ${e.kind}`}>{e.kind_label}</span>
                <div>
                  <b>{e.title}</b>
                  <p className="muted">{e.text}</p>
                </div>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}
