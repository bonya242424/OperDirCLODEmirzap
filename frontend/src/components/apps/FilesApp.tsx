import { ChangeEvent, FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { api, downloadFile, formatAgo, formatDate } from "../../api";
import FileViewer from "./FileViewer";
import type { Folder, Integration, IntegrationsResponse, StoredFile } from "../../types";

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} Б`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} КБ`;
  return `${(bytes / 1024 / 1024).toFixed(1)} МБ`;
}

export default function FilesApp() {
  const [folders, setFolders] = useState<Folder[]>([]);
  const [sources, setSources] = useState<Integration[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [files, setFiles] = useState<StoredFile[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [sourceKey, setSourceKey] = useState("");
  const input = useRef<HTMLInputElement>(null);
  const [viewing, setViewing] = useState<StoredFile | null>(null);

  const fail = (e: unknown) => setError(e instanceof Error ? e.message : "Ошибка");

  const loadFolders = useCallback(() => {
    api<Folder[]>("/folders").then(setFolders).catch(fail);
  }, []);

  const loadFiles = useCallback((id: number) => {
    api<StoredFile[]>(`/folders/${id}/files`).then(setFiles).catch(fail);
  }, []);

  useEffect(() => {
    loadFolders();
    api<IntegrationsResponse>("/integrations")
      .then((r) => setSources(r.items.filter((i) => i.has_sync)))
      .catch(() => {});
  }, [loadFolders]);

  useEffect(() => {
    if (selected !== null) loadFiles(selected);
    else setFiles([]);
  }, [selected, loadFiles]);

  const current = folders.find((f) => f.id === selected) ?? null;

  async function createFolder(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      const f = await api<Folder>("/folders", {
        method: "POST",
        body: { name, description, source_key: sourceKey || null },
      });
      setCreating(false);
      setName("");
      setDescription("");
      setSourceKey("");
      setError("");
      loadFolders();
      setSelected(f.id);
    } catch (err) {
      fail(err);
    } finally {
      setBusy(false);
    }
  }

  async function upload(e: ChangeEvent<HTMLInputElement>) {
    const list = Array.from(e.target.files ?? []);
    e.target.value = "";
    if (!current || list.length === 0) return;
    setBusy(true);
    try {
      for (const file of list) {
        const body = new FormData();
        body.append("file", file);
        await api(`/folders/${current.id}/files`, { method: "POST", body });
      }
      setError("");
    } catch (err) {
      fail(err);
    } finally {
      setBusy(false);
      loadFiles(current.id);
      loadFolders();
    }
  }

  async function remove(file: StoredFile) {
    if (!window.confirm(`Удалить файл «${file.name}»? Это нельзя отменить.`)) return;
    try {
      await api(`/files/${file.id}`, { method: "DELETE" });
      setError("");
      if (current) loadFiles(current.id);
      loadFolders();
    } catch (err) {
      fail(err);
    }
  }

  async function removeFolder(folder: Folder) {
    if (!window.confirm(`Удалить папку «${folder.name}»?`)) return;
    try {
      await api(`/folders/${folder.id}`, { method: "DELETE" });
      setSelected(null);
      setError("");
      loadFolders();
    } catch (err) {
      fail(err);
    }
  }

  return (
    <div className="files">
      {viewing && <FileViewer fileId={viewing.id} name={viewing.name} onClose={() => setViewing(null)} />}
      <aside className="files-side">
        <div className="files-side-head">
          <strong>Папки</strong>
          <button onClick={() => setCreating((v) => !v)}>{creating ? "Отмена" : "Новая"}</button>
        </div>
        {creating && (
          <form className="conn-form" onSubmit={createFolder}>
            <input placeholder="Название" value={name} onChange={(e) => setName(e.target.value)} required />
            <input placeholder="Описание: что это, какие колонки" value={description} onChange={(e) => setDescription(e.target.value)} />
            <label>
              Источник данных
              <select value={sourceKey} onChange={(e) => setSourceKey(e.target.value)}>
                <option value="">Обычная папка</option>
                {sources.map((s) => (
                  <option key={s.key} value={s.key}>{s.name}</option>
                ))}
              </select>
            </label>
            <button type="submit" disabled={busy}>Создать</button>
          </form>
        )}
        {folders.length === 0 && !creating && <p className="muted">Папок пока нет.</p>}
        <ul className="folder-list">
          {folders.map((f) => (
            <li key={f.id}>
              <button className={`folder${f.id === selected ? " on" : ""}`} onClick={() => setSelected(f.id)}>
                <span>📁 {f.name}</span>
                <span className="muted">{f.files_count}</span>
              </button>
            </li>
          ))}
        </ul>
      </aside>
      <section className="files-main">
        {error && <div className="error">{error}</div>}
        {!current ? (
          <p className="muted">Выберите папку слева или создайте новую.</p>
        ) : (
          <>
            <div className="app-head">
              <div>
                <h2>{current.name}</h2>
                {current.description && <p className="muted">{current.description}</p>}
                {current.source_key && (
                  <p className="muted">
                    Источник данных: {sources.find((s) => s.key === current.source_key)?.name ?? current.source_key}.
                    Загрузка файла отмечает свежую выгрузку.
                  </p>
                )}
                {current.last_upload_at && <p className="muted">Последняя загрузка: {formatAgo(current.last_upload_at)}</p>}
              </div>
              <div className="row">
                <input ref={input} type="file" multiple hidden onChange={upload} />
                <button onClick={() => input.current?.click()} disabled={busy}>
                  {busy ? "Загружаем…" : "Загрузить файлы"}
                </button>
                {current.can_edit && current.files_count === 0 && (
                  <button className="ghost" onClick={() => removeFolder(current)}>Удалить папку</button>
                )}
              </div>
            </div>
            {files.length === 0 ? (
              <p className="muted">В папке нет файлов.</p>
            ) : (
              <table className="files-table">
                <thead>
                  <tr><th>Файл</th><th>Размер</th><th>Загрузил</th><th>Когда</th><th /></tr>
                </thead>
                <tbody>
                  {files.map((f) => (
                    <tr key={f.id}>
                      <td><button className="link" onClick={() => setViewing(f)}>{f.name}</button></td>
                      <td>{formatSize(f.size)}</td>
                      <td>{f.uploaded_by}</td>
                      <td title={formatDate(f.uploaded_at)}>{formatAgo(f.uploaded_at)}</td>
                      <td className="row">
                        <button onClick={() => setViewing(f)}>Открыть</button>
                        <button className="ghost" onClick={() => downloadFile(`/files/${f.id}/download`, f.name).catch(fail)}>Скачать</button>
                        {f.can_delete && <button className="ghost" onClick={() => remove(f)}>Удалить</button>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </>
        )}
      </section>
    </div>
  );
}
