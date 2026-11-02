import { ChangeEvent, FormEvent, useCallback, useEffect, useState } from "react";
import { api } from "../../api";

interface Doc {
  id: number;
  supplier: string;
  supplier_id: number | null;
  number: string;
  doc_date: string | null;
  positions: number;
  qty: number;
  invoices: { status: string }[];
  source: string;
}

const iso = (d: Date) => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
const ago = (days: number) => iso(new Date(Date.now() - days * 86400000));

/** Сборка возвратов: все документы по всем поставщикам за период. Собирать из 1С агент будет по инструкции из раздела «Инструкции 1С». */
export default function AssemblyPanel({ onOpen }: { onOpen: (supplierId: number | null) => void }) {
  const [from, setFrom] = useState(ago(30));
  const [to, setTo] = useState(iso(new Date()));
  const [docs, setDocs] = useState<Doc[] | null>(null);
  const [error, setError] = useState("");
  const [link, setLink] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    api<Doc[]>(`/returns/batches?date_from=${from}&date_to=${to}`)
      .then((d) => {
        setDocs(d);
        setError("");
      })
      .catch((e) => setError(e instanceof Error ? e.message : "Ошибка"));
  }, [from, to]);
  useEffect(load, [load]);

  async function upload(e: ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    e.target.value = "";
    if (!f) return;
    const fd = new FormData();
    fd.append("file", f);
    await send(() => api("/returns/import", { method: "POST", body: fd }));
  }

  async function importLink(e: FormEvent) {
    e.preventDefault();
    if (!link.trim()) return;
    await send(() => api("/returns/import-url", { method: "POST", body: { url: link.trim() } }));
    setLink("");
  }

  async function send(fn: () => Promise<unknown>) {
    setBusy(true);
    try {
      await fn();
      setError("");
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Ошибка");
    } finally {
      setBusy(false);
    }
  }

  const by = new Map<string, Doc[]>();
  for (const d of docs ?? []) by.set(d.supplier, [...(by.get(d.supplier) ?? []), d]);

  return (
    <div className="assembly">
      <div className="kanban-bar">
        <h2>Сборка возвратов</h2>
        <div className="row">
          <label className="small muted">с <input type="date" value={from} max={to} onChange={(e) => setFrom(e.target.value)} /></label>
          <label className="small muted">по <input type="date" value={to} min={from} onChange={(e) => setTo(e.target.value)} /></label>
        </div>
      </div>

      <div className="ag-problem" role="status">
        Сбор из 1С агентом ещё не включён: для него нужны вход в 1С и инструкция из раздела «Инструкции 1С». Пока документы можно загрузить файлом или ссылкой на Google Таблицу: поставщик берётся из строки «Поставщик» в документе.
        <div className="ag-upload">
          <button className="primary" disabled title="Нужен вход в 1С и инструкция">Собрать из 1С за период</button>
          <label className="ag-file">
            + Загрузить документ (CSV)
            <input type="file" accept=".csv,text/csv" onChange={upload} disabled={busy} hidden />
          </label>
          <form onSubmit={importLink} className="ag-link">
            <input placeholder="или ссылка на Google Таблицу" value={link} onChange={(e) => setLink(e.target.value)} />
            <button disabled={busy || !link.trim()}>Загрузить</button>
          </form>
        </div>
      </div>

      {error && <p className="error">{error}</p>}
      {docs && docs.length === 0 && <p className="ag-hint">За выбранный период документов нет. Измените даты или загрузите документ.</p>}
      {[...by.entries()].map(([supplier, list]) => (
        <section key={supplier}>
          <h4>{supplier}</h4>
          <table className="ag-table">
            <thead><tr><th>Дата</th><th>Документ</th><th>Позиций</th><th>Штук</th><th>Счетов</th><th>Проверено</th><th /></tr></thead>
            <tbody>
              {list.map((d) => {
                const checked = d.invoices.filter((i) => i.status !== "pending").length;
                return (
                  <tr key={d.id}>
                    <td>{d.doc_date ? new Date(d.doc_date).toLocaleDateString("ru-RU") : "—"}</td>
                    <td>{d.number || `№${d.id}`}</td>
                    <td>{d.positions}</td>
                    <td>{d.qty}</td>
                    <td>{d.invoices.length}</td>
                    <td>{checked} из {d.invoices.length}</td>
                    <td><button className="ghost" onClick={() => onOpen(d.supplier_id)}>Открыть у поставщика</button></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </section>
      ))}
    </div>
  );
}
