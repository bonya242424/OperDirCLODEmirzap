import { useEffect, useState } from "react";
import { api, downloadFile } from "../../api";

interface View {
  name: string;
  kind: "table" | "text";
  rows?: string[][];
  total?: number;
  text?: string;
  truncated: boolean;
}

/** Просмотр файла прямо в системе: таблица CSV или текст. Для сборок из 1С — связь с документом возврата. */
export default function FileViewer({ fileId, name, onClose, onOpenReturn }: { fileId: number; name: string; onClose: () => void; onOpenReturn?: (supplierId: number | null) => void }) {
  const [view, setView] = useState<View | null>(null);
  const [error, setError] = useState("");
  const [ret, setRet] = useState<{ batch_id: number | null; supplier_id: number | null } | null>(null);
  const [busy, setBusy] = useState(false);
  const assembly = name.startsWith("Сборка ");

  useEffect(() => {
    api<View>(`/files/${fileId}/view`).then(setView).catch((e) => setError(e instanceof Error ? e.message : "Ошибка"));
    if (assembly) api<{ batch_id: number | null; supplier_id: number | null }>(`/returns/from-file/${fileId}`).then(setRet).catch(() => setRet(null));
  }, [fileId, assembly]);

  async function makeReturn() {
    setBusy(true);
    setError("");
    try {
      const b = await api<{ id: number; supplier_id: number | null }>(`/returns/from-file/${fileId}`, { method: "POST" });
      setRet({ batch_id: b.id, supplier_id: b.supplier_id });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Ошибка");
    } finally {
      setBusy(false);
    }
  }

  const rows = view?.rows ?? [];
  const width = rows.reduce((m, r) => Math.max(m, r.length), 0);
  return (
    <div className="modal-bg" onPointerDown={onClose}>
      <div className="modal file-view" onPointerDown={(e) => e.stopPropagation()}>
        <div className="modal-actions">
          <h3 className="grow">{name}</h3>
          <button onClick={onClose}>Закрыть</button>
        </div>
        {assembly && ret && (
          <div className="row file-view-return">
            {ret.batch_id ? (
              <>
                <span className="muted small">Документ возврата создан. Возврат на сайте поставщика запускается кнопкой внутри документа.</span>
                {onOpenReturn && <button className="primary" onClick={() => onOpenReturn(ret.supplier_id)}>Открыть возврат</button>}
                <button className="ghost" disabled={busy} onClick={makeReturn} title="Пересоздать документ по этому файлу">Обновить</button>
              </>
            ) : (
              <>
                <span className="muted small">Документа возврата по этой сборке ещё нет.</span>
                <button className="primary" disabled={busy} onClick={makeReturn}>{busy ? "Создаю…" : "Создать документ возврата"}</button>
              </>
            )}
          </div>
        )}
        {error && <p className="error">{error}</p>}
        {!view && !error && <p className="muted">Открываю…</p>}
        {view?.kind === "table" && (
          <div className="file-view-scroll">
            <table className="files-table">
              <tbody>
                {rows.map((r, i) => (
                  <tr key={i}>{Array.from({ length: width }, (_, j) => <td key={j}>{r[j] ?? ""}</td>)}</tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {view?.kind === "text" && <pre className="file-view-scroll file-view-text">{view.text}</pre>}
        {view?.truncated && <p className="muted small">Показано начало файла{view.total ? ` (${rows.length} из ${view.total} строк)` : ""}. Полностью — в скачанном файле.</p>}
        <div className="modal-actions">
          <span className="grow" />
          <button className="ghost" onClick={() => downloadFile(`/files/${fileId}/download`, name).catch(() => undefined)}>Скачать</button>
        </div>
      </div>
    </div>
  );
}
