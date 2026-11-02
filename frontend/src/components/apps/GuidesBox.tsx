import { ChangeEvent, useCallback, useEffect, useState } from "react";
import { api, formatAgo } from "../../api";

interface Guide {
  id: number;
  filename: string;
  size: number;
  status: "running" | "ok" | "error";
  text: string;
  message: string;
  created_at: string;
}

const STATUS: Record<Guide["status"], string> = { running: "Разбираю кадры…", ok: "Инструкция готова", error: "Не получилось разобрать" };
const mb = (n: number) => `${(n / 1048576).toFixed(1)} МБ`;

interface BoxProps {
  /** Адрес списка и загрузки видео, например /returns/suppliers/3/guides */
  listUrl: string;
  /** Адрес элемента без id, например /returns/guides */
  itemUrl: string;
  title?: string;
  hint?: string;
  canEdit?: boolean;
}

export default function GuidesBox({ listUrl, itemUrl, title = "Видео «как делать возврат»", hint, canEdit = true }: BoxProps) {
  const [items, setItems] = useState<Guide[] | null>(null);
  const [drafts, setDrafts] = useState<Record<number, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    api<Guide[]>(listUrl).then(setItems).catch((e) => setError(e instanceof Error ? e.message : "Ошибка"));
  }, [listUrl]);

  useEffect(() => {
    setItems(null);
    setDrafts({});
    load();
  }, [load]);

  const running = items?.some((g) => g.status === "running") ?? false;
  useEffect(() => {
    if (!running) return;
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [running, load]);

  const fail = (e: unknown) => setError(e instanceof Error ? e.message : "Ошибка");

  async function upload(e: ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    e.target.value = "";
    if (!f) return;
    const fd = new FormData();
    fd.append("file", f);
    setBusy(true);
    setError("");
    try {
      await api(listUrl, { method: "POST", body: fd });
      load();
    } catch (err) {
      fail(err);
    } finally {
      setBusy(false);
    }
  }

  async function act(fn: () => Promise<unknown>) {
    try {
      await fn();
      setError("");
      load();
    } catch (err) {
      fail(err);
    }
  }

  return (
    <div className="guides">
      <div className="guides-head">
        <h4>{title}</h4>
        {canEdit && (
          <label className="ag-file">
            + Загрузить видео
            <input type="file" accept="video/*,.mp4,.mov,.webm,.mkv" onChange={upload} disabled={busy} hidden />
          </label>
        )}
      </div>
      <p className="muted small">{hint ?? "Запишите экран, как вы оформляете возврат на сайте. По кадрам ИИ составит пошаговую инструкцию для агента, а вы сможете её поправить. Звук не распознаётся: важное, что сказано голосом, допишите в текст."}</p>
      {error && <p className="error">{error}</p>}
      {items?.map((g) => {
        const text = drafts[g.id] ?? g.text;
        return (
          <div key={g.id} className={`guide ${g.status}`}>
            <div className="guide-top">
              <b>{g.filename}</b>
              <span className="muted small">{mb(g.size)} · {formatAgo(g.created_at)} · {STATUS[g.status]}</span>
              <span className="grow" />
              {canEdit && <button className="ghost" disabled={g.status === "running"} onClick={() => act(() => api(`${itemUrl}/${g.id}/reanalyze`, { method: "POST" }))}>Разобрать снова</button>}
              {canEdit && <button className="ghost danger" onClick={() => window.confirm(`Удалить видео «${g.filename}»?`) && act(() => api(`${itemUrl}/${g.id}`, { method: "DELETE" }))}>Удалить</button>}
            </div>
            {g.message && g.status !== "running" && <p className={g.status === "error" ? "error" : "muted small"}>{g.message}</p>}
            {g.status === "ok" && (
              <>
                <textarea rows={10} value={text} onChange={(e) => setDrafts({ ...drafts, [g.id]: e.target.value })} />
                {drafts[g.id] !== undefined && drafts[g.id] !== g.text && (
                  <button className="primary" onClick={() => act(async () => { await api(`${itemUrl}/${g.id}`, { method: "PATCH", body: { text: drafts[g.id] } }); setDrafts((d) => { const { [g.id]: _drop, ...rest } = d; return rest; }); })}>
                    Сохранить правки
                  </button>
                )}
              </>
            )}
          </div>
        );
      })}
      {items && items.length === 0 && <p className="muted small">Видео пока нет.</p>}
    </div>
  );
}
