import { FormEvent, useCallback, useEffect, useState } from "react";
import { api } from "../../api";
import type { User } from "../../types";
import GuidesBox from "./GuidesBox";

interface Section {
  id: number;
  title: string;
  notes: string;
  videos: number;
}

/** Инструкции 1С: владелец создаёт разделы, описывает, что делает, и грузит видео. ИИ составляет шаги для агента. */
export default function InstructionsApp({ me }: { me: User }) {
  const owner = me.role === "owner";
  const [sections, setSections] = useState<Section[] | null>(null);
  const [sel, setSel] = useState<number | null>(null);
  const [title, setTitle] = useState("");
  const [adding, setAdding] = useState(false);
  const [notes, setNotes] = useState("");
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    api<Section[]>("/instructions/sections")
      .then((s) => {
        setSections(s);
        setSel((cur) => (cur !== null && s.some((x) => x.id === cur) ? cur : s[0]?.id ?? null));
      })
      .catch((e) => setError(e instanceof Error ? e.message : "Ошибка"));
  }, []);
  useEffect(load, [load]);

  const current = sections?.find((s) => s.id === sel) ?? null;
  useEffect(() => {
    setNotes(current?.notes ?? "");
    setDirty(false);
  }, [current?.id, current?.notes]);

  async function create(e: FormEvent) {
    e.preventDefault();
    if (!title.trim()) return;
    try {
      const s = await api<Section>("/instructions/sections", { method: "POST", body: { title } });
      setTitle("");
      setAdding(false);
      setSel(s.id);
      setError("");
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Ошибка");
    }
  }

  async function saveNotes() {
    if (!current) return;
    try {
      await api(`/instructions/sections/${current.id}`, { method: "PATCH", body: { notes } });
      setDirty(false);
      setError("");
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Ошибка");
    }
  }

  async function remove() {
    if (!current || !window.confirm(`Удалить раздел «${current.title}» вместе с видео и инструкциями?`)) return;
    try {
      await api(`/instructions/sections/${current.id}`, { method: "DELETE" });
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Ошибка");
    }
  }

  return (
    <div className="agents">
      <aside className="ag-left">
        <h3>Инструкции 1С</h3>
        <p className="muted small">Создайте раздел под одно действие в 1С, опишите его своими словами и загрузите видео. Из этого агент поймёт, как работать в 1С.</p>
        <ul>
          {sections?.map((s) => (
            <li key={s.id}>
              <button className={s.id === sel ? "on" : ""} onClick={() => setSel(s.id)}>
                <strong>{s.title}</strong>
                <span className="muted small">{s.videos} видео</span>
              </button>
            </li>
          ))}
        </ul>
        {sections && sections.length === 0 && <p className="muted small">Разделов пока нет.</p>}
        {owner && (adding ? (
          <form onSubmit={create} className="ag-link">
            <input autoFocus placeholder="Например «Сборка возвратов»" value={title} onChange={(e) => setTitle(e.target.value)} />
            <button disabled={!title.trim()}>Создать</button>
          </form>
        ) : (
          <button className="primary" onClick={() => setAdding(true)}>+ Раздел</button>
        ))}
        {error && <p className="error">{error}</p>}
      </aside>
      <section className="ag-main">
        {!current ? (
          <p className="ag-hint">Выберите раздел слева или создайте новый. Например: «Сборка возвратов», «Отчёт по продажам», «Остатки по складам».</p>
        ) : (
          <>
            <header>
              <div>
                <h2>{current.title}</h2>
              </div>
              <div className="ag-actions">{owner && <button className="ghost danger" onClick={remove}>Удалить раздел</button>}</div>
            </header>
            <label className="grow">
              Что вы делаете в этом разделе своими словами
              <textarea
                rows={6}
                value={notes}
                disabled={!owner}
                placeholder="Например: открываю «Журнал документов», ставлю фильтр по дате, выбираю документы «Сборка», открываю каждый и сохраняю список по поставщикам. Звук из видео не распознаётся, поэтому важное пишите здесь."
                onChange={(e) => {
                  setNotes(e.target.value);
                  setDirty(true);
                }}
              />
            </label>
            {dirty && <button className="primary" onClick={saveNotes}>Сохранить описание</button>}
            <GuidesBox
              key={current.id}
              listUrl={`/instructions/sections/${current.id}/videos`}
              itemUrl="/instructions/videos"
              title="Видео: как это делается в 1С"
              hint="Запишите экран, как вы делаете это в 1С. По кадрам ИИ составит пошаговую инструкцию (какое меню, какие фильтры, какие кнопки), её можно поправить. Описание раздела выше учитывается."
              canEdit={owner}
            />
          </>
        )}
      </section>
    </div>
  );
}
