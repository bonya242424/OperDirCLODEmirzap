import { ChangeEvent, FormEvent, useCallback, useEffect, useState } from "react";
import { api, getToken } from "../../api";
import type { User } from "../../types";
import AgentList from "./AgentList";
import AgentStructure from "./AgentStructure";
import AssemblyPanel from "./AssemblyPanel";
import BrowserLogin from "./BrowserLogin";
import GuidesBox from "./GuidesBox";

type StepState = "wait" | "run" | "confirm" | "done" | "skip";
interface Step {
  title: string;
  detail: string;
  needsConfirm?: boolean;
  state: StepState;
}

interface Supplier {
  id: number;
  name: string;
  portal_url: string;
  contact: string;
  rules: string;
  default_reason: string;
  notes: string;
  batches: number;
  positions: number;
  has_session: boolean;
  session_saved_at: string | null;
}
type SupplierForm = Omit<Supplier, "id" | "batches" | "positions" | "has_session" | "session_saved_at"> & { id: number | null };
const EMPTY_SUP: SupplierForm = { id: null, name: "", portal_url: "", contact: "", rules: "", default_reason: "", notes: "" };

interface Run {
  id: number;
  batch_id: number;
  status: "running" | "done" | "failed" | "stopped";
  summary: string;
  has_shot: boolean;
  log: string[];
  created_at: string;
}
function RunShot({ runId }: { runId: number }) {
  const [url, setUrl] = useState("");
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!open) return;
    let made = "";
    fetch(`/api/returns/runs/${runId}/shot`, { headers: { Authorization: `Bearer ${getToken() ?? ""}` } })
      .then((r) => (r.ok ? r.blob() : Promise.reject(new Error("Скриншота нет"))))
      .then((b) => {
        made = URL.createObjectURL(b);
        setUrl(made);
      })
      .catch(() => setUrl(""));
    return () => {
      if (made) URL.revokeObjectURL(made);
    };
  }, [open, runId]);
  return (
    <div>
      <button className="ghost" onClick={() => setOpen((v) => !v)}>{open ? "Скрыть, что видел агент" : "Что видел агент в конце"}</button>
      {open && (url ? <img className="run-shot" src={url} alt="Последний экран агента" /> : <p className="muted small">Загружаю…</p>)}
    </div>
  );
}

const INV_STATUS: Record<string, string> = { pending: "не проверен", checked: "можно оформить", failed: "есть проблема", submitted: "отправлен", skipped: "пропущен" };

interface Summary {
  id: number;
  supplier_id: number | null;
  doc_date: string | null;
  supplier: string;
  number: string;
  source: string;
  positions: number;
  qty: number;
  invoices: { invoice_no: string; positions: number; qty: number; date: string; status: string; note: string }[];
  warehouses: { warehouse: string; positions: number; qty: number }[];
  reasons: { reason: string; qty: number }[];
  status: Record<string, number>;
  problems: string[];
}

interface ReturnItem {
  id: number;
  article: string;
  name: string;
  brand: string;
  qty: number;
  warehouse: string;
  invoice_date: string;
  invoice_no: string;
  reason: string;
  status: string;
  request_no: string;
  note: string;
}

const TEMPLATE: Omit<Step, "state">[] = [
  { title: "Открыть «Оформление возврата»", detail: "Вход уже выполнен вами в браузере; агент пароли не вводит" },
  { title: "Найти товар по артикулу", detail: "Сверяю номер и дату счёта по найденной фактуре" },
  { title: "Выставить количество «К возврату»", detail: "Для каждой позиции счёта количество берётся из таблицы" },
  { title: "Заполнить окно возврата", detail: "Причина из таблицы; адрес и контактное лицо сайт подставляет сам; файлы не нужны" },
  { title: "Отправить заявку", detail: "Необратимое действие: заявку нельзя отозвать", needsConfirm: true },
  { title: "Записать номер заявки в таблицу", detail: "Номер заявки и статус вносятся в строки счёта" },
];


/** Агент по возвратам. Таблица и саммари настоящие; шаги работы на сайте пока показаны для примера: запуск на сайте подключим после первой проверки. */
function ReturnsPanel({ me, focusSupplier }: { me: User; focusSupplier: number | null }) {
  const [suppliers, setSuppliers] = useState<Supplier[] | null>(null);
  const supplierKey = `operdir_returns_supplier_${me.id}`;
  const [supSel, setSupSel] = useState<number | null>(() => focusSupplier ?? (Number(sessionStorage.getItem(supplierKey)) || null));
  useEffect(() => {
    if (supSel !== null) sessionStorage.setItem(supplierKey, String(supSel));
  }, [supplierKey, supSel]);
  const [supForm, setSupForm] = useState<SupplierForm | null>(null);
  const [loginOpen, setLoginOpen] = useState(false);
  const [allBatches, setBatches] = useState<Summary[] | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [link, setLink] = useState("");
  const [busy, setBusy] = useState(false);
  const running = false;
  const [multi, setMulti] = useState(false);
  const [picked, setPicked] = useState<number[]>([]);
  const [queue, setQueue] = useState<{ active: boolean; active_suppliers?: number[]; total: number; pos: number; current: { id: number; number: string } | null; done: { batch_id: number; status: string }[] } | null>(null);
  const [info, setInfo] = useState("");
  const supplier = suppliers?.find((x) => x.id === supSel) ?? null;
  const [dFrom, setDFrom] = useState("");
  const [dTo, setDTo] = useState("");
  const batches =
    allBatches?.filter((b) => b.supplier_id === supSel && (!dFrom || (b.doc_date ?? "") >= dFrom) && (!dTo || (b.doc_date ?? "") <= dTo)) ?? null;
  const current = batches?.find((b) => b.id === selected) ?? null;

  const load = useCallback(() => {
    Promise.all([api<Supplier[]>("/returns/suppliers"), api<Summary[]>("/returns/batches")])
      .then(([sup, b]) => {
        setSuppliers(sup);
        setBatches(b);
        setSupSel((cur) => (cur !== null && sup.some((x) => x.id === cur) ? cur : sup[0]?.id ?? null));
      })
      .catch((e) => setError(e instanceof Error ? e.message : "Ошибка"));
  }, []);
  useEffect(load, [load]);
  useEffect(() => {
    setSelected((cur) => (cur !== null && batches?.some((x) => x.id === cur) ? cur : batches?.[0]?.id ?? null));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [supSel, allBatches]);

  async function upload(e: ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    e.target.value = "";
    if (!f) return;
    const fd = new FormData();
    fd.append("file", f);
    if (!supplier) return;
    await send(() => api<Summary>(`/returns/import?supplier_id=${supplier.id}`, { method: "POST", body: fd }));
  }

  async function importLink(e: FormEvent) {
    e.preventDefault();
    if (!link.trim() || !supplier) return;
    await send(() => api<Summary>("/returns/import-url", { method: "POST", body: { url: link.trim(), supplier_id: supplier.id } }));
    setLink("");
  }

  async function send(fn: () => Promise<Summary>) {
    setBusy(true);
    setError("");
    try {
      const s = await fn();
      setSelected(s.id);
      reset();
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Ошибка");
    } finally {
      setBusy(false);
    }
  }

  const reset = () => undefined;
  const [runsBySupplier, setRunsBySupplier] = useState<Partial<Record<number, Run[]>>>({});
  const runs = supSel === null ? undefined : runsBySupplier[supSel];
  const activeRun = runs?.find((run) => run.status === "running");
  const lastRun = activeRun ?? runs?.[0] ?? null;
  const checking = !!activeRun;

  const batchId = current?.id;
  const [itemResult, setItemResult] = useState<{ batchId: number; rows?: ReturnItem[]; error?: string }>();
  useEffect(() => {
    if (batchId === undefined) return;
    let stale = false;
    api<ReturnItem[]>(`/returns/batches/${batchId}/lines`)
      .then((rows) => { if (!stale) setItemResult({ batchId, rows }); })
      .catch((err) => {
        if (!stale) setItemResult({ batchId, error: err instanceof Error ? err.message : "Не удалось загрузить товары" });
      });
    return () => { stale = true; };
  }, [batchId, allBatches, lastRun?.id, lastRun?.status]);

  const [runsError, setRunsError] = useState("");
  const loadRuns = useCallback(() => {
    if (supSel === null) return;
    api<Run[]>(`/returns/suppliers/${supSel}/runs`)
      .then((r) => {
        setRunsBySupplier((previous) => ({ ...previous, [supSel]: r }));
      })
      .catch((e) => setRunsError(e instanceof Error ? e.message : "Не удалось загрузить журнал"));
  }, [supSel]);
  useEffect(() => {
    loadRuns();
    const t = setInterval(loadRuns, 2500);
    return () => clearInterval(t);
  }, [loadRuns]);
  useEffect(() => {
    if (!checking) return;
    const t = setInterval(load, 2500);
    return () => clearInterval(t);
  }, [checking, load]);

  useEffect(() => {
    if (me.role !== "owner" || !supSel) return;
    const url = `/returns/check-queue?supplier_id=${supSel}`;
    const t = window.setInterval(() => api<NonNullable<typeof queue>>(url).then(setQueue).catch(() => undefined), 3000);
    api<NonNullable<typeof queue>>(url).then(setQueue).catch(() => undefined);
    return () => window.clearInterval(t);
  }, [me.role, supSel]);

  function togglePick(id: number) {
    setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id]));
  }

  async function startQueue() {
    setError("");
    setInfo("");
    try {
      setQueue(await api("/returns/check-queue", { method: "POST", body: { batch_ids: picked } }));
      setMulti(false);
      setPicked([]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Ошибка");
    }
  }

  async function cancelQueue() {
    try {
      setQueue(await api(`/returns/check-queue/cancel?supplier_id=${supSel}`, { method: "POST" }));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Ошибка");
    }
  }

  async function dedupe() {
    setError("");
    try {
      const r = await api<{ removed: number; skipped: number }>("/returns/dedupe", { method: "POST" });
      setInfo(r.removed ? `Убрано дублей: ${r.removed}${r.skipped ? `, оставлено ${r.skipped} (по ним уже есть результаты)` : ""}` : "Дублей нет");
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Ошибка");
    }
  }

  async function startCheck() {
    if (!current || !runs || activeRun || busy || supSel === null) return;
    setError("");
    setRunsError("");
    setInfo("");
    if (!(current.status.pending ?? 0)) {
      setRunsError((current.status.failed ?? 0) > 0
        ? "Непроверенных строк нет. Нажмите «Сбросить ошибки», затем запустите проверку."
        : "Все строки сборки уже проверены.");
      return;
    }
    setBusy(true);
    try {
      const run = await api<Run>(`/returns/batches/${current.id}/check-run`, { method: "POST" });
      setRunsBySupplier((previous) => ({ ...previous, [supSel]: [run, ...(previous[supSel] ?? []).filter((item) => item.id !== run.id)] }));
      loadRuns();
    } catch (err) {
      setRunsError(err instanceof Error ? err.message : "Ошибка");
    } finally {
      setBusy(false);
    }
  }

  async function resetErrors() {
    if (!current || busy) return;
    setBusy(true);
    setError("");
    setInfo("");
    try {
      const result = await api<{ reset: number }>(`/returns/batches/${current.id}/reset-errors`, { method: "POST" });
      setInfo(`Сброшено ошибок: ${result.reset}. Запустите проверку сборки заново.`);
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось сбросить ошибки");
    } finally {
      setBusy(false);
    }
  }

  async function stopCheck() {
    if (!activeRun) return;
    try {
      await api(`/returns/runs/${activeRun.id}/stop`, { method: "POST" });
      loadRuns();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Ошибка");
    }
  }

  async function remove(b: Summary) {
    if (!window.confirm(`Удалить сборку ${b.number || b.id} из системы? Сама таблица не изменится.`)) return;
    try {
      await api(`/returns/batches/${b.id}`, { method: "DELETE" });
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Ошибка");
    }
  }

  async function saveSupplier(e: FormEvent) {
    e.preventDefault();
    if (!supForm) return;
    const { id, ...body } = supForm;
    try {
      const saved = id === null ? await api<Supplier>("/returns/suppliers", { method: "POST", body }) : await api<Supplier>(`/returns/suppliers/${id}`, { method: "PATCH", body });
      setSupForm(null);
      setSupSel(saved.id);
      setError("");
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Ошибка");
    }
  }

  async function removeSupplier(x: Supplier) {
    if (!window.confirm(`Убрать поставщика «${x.name}»? Загруженные документы останутся в базе.`)) return;
    try {
      await api(`/returns/suppliers/${x.id}`, { method: "DELETE" });
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Ошибка");
    }
  }

  const done = current?.status.submitted ?? 0;

  return (
    <div className="agents">
      <aside className="ag-left">
        <h3>Поставщики</h3>
        <p className="muted small">Создайте поставщика, загрузите в него документ с возвратами и запустите работу.</p>
        <ul>
          {suppliers?.map((x) => (
            <li key={x.id}>
              <button className={x.id === supSel ? "on" : ""} onClick={() => { setSupSel(x.id); reset(); }} disabled={running}>
                <strong>{x.name}</strong>
                <span className="muted small">{x.batches} док. · в последнем {x.positions} поз.</span>
              </button>
            </li>
          ))}
        </ul>
        {suppliers && suppliers.length === 0 && <p className="muted small">Поставщиков пока нет.</p>}
        <button className="primary" onClick={() => setSupForm({ ...EMPTY_SUP })}>+ Поставщик</button>
        {error && <p className="error">{error}</p>}
      </aside>

      <section className="ag-main">
        {!supplier ? (
          <p className="ag-hint">Создайте первого поставщика кнопкой «+ Поставщик» слева: название, сайт кабинета, контакты и правила возврата.</p>
        ) : (
          <>
            <header>
              <div>
                <h2>{supplier.name}</h2>
                <p className="muted small">
                  {supplier.portal_url ? <a href={supplier.portal_url} target="_blank" rel="noreferrer noopener">{supplier.portal_url}</a> : "Сайт кабинета не указан"}
                  {supplier.contact && <> · {supplier.contact}</>}
                </p>
              </div>
              <div className="ag-actions">
                <button className="ghost" onClick={() => removeSupplier(supplier)} disabled={running}>Убрать</button>
                <button onClick={() => setSupForm({ ...supplier })} disabled={running}>Данные поставщика</button>
              </div>
            </header>
            {supplier.rules && <p className="muted small">Правила: {supplier.rules}</p>}
            {me.role === "owner" && (
              <div className="ag-session">
                <span className={supplier.has_session ? "good" : "muted"}>
                  {supplier.has_session ? `Вход в кабинет сохранён ${supplier.session_saved_at ? new Date(supplier.session_saved_at).toLocaleString("ru-RU") : ""}` : "Вход в кабинет не выполнен"}
                </span>
                <button onClick={() => setLoginOpen(true)} disabled={!supplier.portal_url} title={supplier.portal_url ? "" : "Укажите адрес кабинета в данных поставщика"}>
                  {supplier.has_session ? "Войти заново" : "Войти в кабинет"}
                </button>
              </div>
            )}
            <div className="ag-upload">
              <label className="ag-file">
                + Загрузить CSV
                <input type="file" accept=".csv,text/csv" onChange={upload} disabled={busy} hidden />
              </label>
              <form onSubmit={importLink} className="ag-link">
                <input placeholder="или ссылка на Google Таблицу" value={link} onChange={(e) => setLink(e.target.value)} />
                <button disabled={busy || !link.trim()}>Загрузить</button>
              </form>
            </div>
            {error && <p className="error">{error}</p>}
            <GuidesBox listUrl={`/returns/suppliers/${supplier.id}/guides`} itemUrl="/returns/guides" />
            <div className="row small muted">
              Период сборки:
              <input type="date" value={dFrom} max={dTo || undefined} onChange={(e) => setDFrom(e.target.value)} aria-label="Сборка с" />
              <input type="date" value={dTo} min={dFrom || undefined} onChange={(e) => setDTo(e.target.value)} aria-label="Сборка по" />
              {(dFrom || dTo) && <button className="ghost" onClick={() => { setDFrom(""); setDTo(""); }}>Сбросить</button>}
            </div>
            {queue?.active && (
              <div className="ag-hint">
                Очередь проверки: документ {queue.pos} из {queue.total}{queue.current ? ` (${queue.current.number || queue.current.id})` : ""}.
                {(queue.active_suppliers?.length ?? 0) > 1 && ` Одновременно идут поставщиков: ${queue.active_suppliers?.length}.`}
                <button className="ghost danger" onClick={cancelQueue}>Отменить очередь</button>
              </div>
            )}
            {info && <p className="ok-text">{info}</p>}
            {batches && batches.length > 0 && me.role === "owner" && (
              <div className="row small">
                <button className="ghost" onClick={() => { setMulti((m) => !m); setPicked([]); }} disabled={!!queue?.active}>{multi ? "Выход из выбора" : "Выбрать несколько"}</button>
                {multi && <button className="ghost" onClick={() => setPicked(batches.map((b) => b.id))}>Выбрать все</button>}
                {multi && (
                  <button className="primary" onClick={startQueue} disabled={picked.length === 0 || !supplier?.has_session} title={supplier?.has_session ? "Агент проверит выбранные документы по очереди, ничего не отправляя" : "Сначала войдите в кабинет и сохраните вход"}>
                    Проверить выбранные ({picked.length})
                  </button>
                )}
                {!multi && <button className="ghost" onClick={dedupe}>Убрать дубли</button>}
              </div>
            )}
            {batches && batches.length > 0 && (
              <div className="chips">
                {batches.map((b) => (
                  multi ? (
                    <label key={b.id} className={`chip${picked.includes(b.id) ? " on" : ""}`}>
                      <input type="checkbox" checked={picked.includes(b.id)} onChange={() => togglePick(b.id)} /> {b.number || `Документ ${b.id}`} · {b.positions} поз.
                    </label>
                  ) : (
                    <button key={b.id} className={`chip${b.id === selected ? " on" : ""}`} onClick={() => { setSelected(b.id); reset(); }} disabled={running}>
                      {b.number || `Документ ${b.id}`} · {b.positions} поз.
                    </button>
                  )
                ))}
              </div>
            )}
            {batches && batches.length === 0 && (
              <p className="ag-hint">Документов пока нет. Загрузите CSV или вставьте ссылку на Google Таблицу. Нужны колонки: Товар, Кол. по документу, Номер счета, причина возврата.</p>
            )}
          </>
        )}
        {supplier && current && (
          <>
            <header>
              <div>
                <h3>Документ {current.number || current.id}</h3>
                <p className="muted small">Источник: {current.source}</p>
              </div>
              <div className="ag-actions">
                <button className="ghost" onClick={() => remove(current)} disabled={running}>Удалить</button>
                {me.role === "owner" && (current.status.failed ?? 0) > 0 && (
                  <button onClick={resetErrors} disabled={busy || !runs || checking || !!queue?.active}>
                    Сбросить ошибки
                  </button>
                )}
                {me.role === "owner" && (checking ? (
                  <button className="danger" onClick={stopCheck}>Остановить{activeRun?.batch_id !== current.id ? " проверку другой сборки" : ""}</button>
                ) : (
                  <button className="primary" onClick={startCheck} disabled={busy || !runs || !supplier?.has_session} title={supplier?.has_session ? "Агент найдёт счета на сайте и проверит, можно ли вернуть. Ничего не отправляет" : "Сначала войдите в кабинет и сохраните вход"}>
                    {busy ? "Запускаю проверку…" : "Проверить счета на сайте"}
                  </button>
                ))}
              </div>
            </header>

            {runsError && <p className="error" role="alert">{runsError}</p>}
            <div className="ag-sum">
              <div><b>{current.positions}</b><span>позиций</span></div>
              <div><b>{current.qty}</b><span>штук</span></div>
              <div><b>{current.invoices.length}</b><span>счетов = заявок</span></div>
              <div><b>{done}</b><span>отправлено</span></div>
            </div>
            {current.problems.map((p) => (
              <p key={p} className="ag-problem">{p}</p>
            ))}

            <h4>Товары в сборке</h4>
            {itemResult?.batchId === current.id && itemResult.error && <p className="error">{itemResult.error}</p>}
            {itemResult?.batchId === current.id && itemResult.rows ? (
              <table className="ag-table">
                <thead><tr><th>Артикул</th><th>Наименование</th><th>Производитель</th><th>К возврату, шт.</th><th>Склад возврата</th><th>Дата счёта</th><th>Номер счёта</th><th>Причина возврата</th><th>Проверка на сайте</th><th>Номер заявки</th></tr></thead>
                <tbody>
                  {itemResult.rows.map((item) => (
                    <tr key={item.id}>
                      <td>{item.article}</td><td>{item.name}</td><td>{item.brand}</td>
                      <td>{item.qty}</td><td>{item.warehouse}</td>
                      <td>{item.invoice_date}</td><td>{item.invoice_no}</td><td>{item.reason}</td>
                      <td className={`inv-${item.status}`}>{INV_STATUS[item.status] ?? item.status}{item.note && <span className="muted small"> · {item.note}</span>}</td>
                      <td>{item.request_no}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : itemResult?.batchId !== current.id && <p className="muted small">Загружаю товары…</p>}
            <p className="muted small">
              Причина: {current.reasons.map((r) => `${r.reason} (${r.qty} шт.)`).join(", ")}. Склады: {current.warehouses.map((w) => `${w.warehouse} ${w.qty}`).join(", ")}.
            </p>

            <div className="ag-problem" role="status">
              Пока агент только проверяет счета на сайте: что найден, не возвращён ли ранее, сходится ли количество. Оформление и отправка заявок ещё не включены, кнопки отправки заблокированы.
            </div>
            {lastRun && lastRun.status !== "running" && lastRun.summary && <p className={lastRun.status === "failed" ? "error" : "muted"}>{lastRun.summary}</p>}
            <h4>Что делает агент</h4>
            <ol className="ag-steps">
              {TEMPLATE.map((s, i) => (
                <li key={i} className="st-wait">
                  <span className="ag-ic">○</span>
                  <div>
                    <strong>{s.title}</strong>
                    <p className="muted small">{s.detail}</p>
                  </div>
                </li>
              ))}
            </ol>
          </>
        )}
      </section>

      {loginOpen && supplier && (
        <BrowserLogin supplierId={supplier.id} name={supplier.name} onClose={() => { setLoginOpen(false); load(); }} />
      )}
      {supForm && (
        <div className="modal-bg" onPointerDown={() => setSupForm(null)}>
          <form className="modal" onSubmit={saveSupplier} onPointerDown={(e) => e.stopPropagation()}>
            <h3>{supForm.id === null ? "Новый поставщик" : "Данные поставщика"}</h3>
            <input autoFocus required placeholder="Название, например «Армтек»" value={supForm.name} onChange={(e) => setSupForm({ ...supForm, name: e.target.value })} />
            <input placeholder="Адрес кабинета, https://etp.armtek.ru" value={supForm.portal_url} onChange={(e) => setSupForm({ ...supForm, portal_url: e.target.value })} />
            <textarea rows={2} placeholder="Контакты: менеджер, телефон, почта" value={supForm.contact} onChange={(e) => setSupForm({ ...supForm, contact: e.target.value })} />
            <input placeholder="Причина возврата по умолчанию (если в таблице пусто)" value={supForm.default_reason} onChange={(e) => setSupForm({ ...supForm, default_reason: e.target.value })} />
            <textarea rows={4} placeholder="Как оформлять возврат на этом сайте: одна заявка на счёт, что подставляется само, когда спросить владельца" value={supForm.rules} onChange={(e) => setSupForm({ ...supForm, rules: e.target.value })} />
            <textarea rows={2} placeholder="Заметки" value={supForm.notes} onChange={(e) => setSupForm({ ...supForm, notes: e.target.value })} />
            <p className="muted small">Логин и пароль кабинета здесь не хранятся: вход в кабинет выполняете вы сами.</p>
            {error && <p className="error">{error}</p>}
            <div className="modal-actions">
              <span className="grow" />
              <button type="button" onClick={() => setSupForm(null)}>Отмена</button>
              <button type="submit" className="primary">Сохранить</button>
            </div>
          </form>
        </div>
      )}

      <aside className="ag-log">
        <h3>Журнал запуска</h3>

        {!lastRun ? (
          <p className="muted small">Запусков ещё не было. Нажмите «Проверить счета на сайте»: здесь появится каждое действие агента.</p>
        ) : (
          <>
            <p className="muted small">{checking ? "Идёт проверка…" : { done: "Проверка завершена", failed: "Проверка не удалась", stopped: "Остановлена", running: "" }[lastRun.status]}</p>
            {lastRun.has_shot && !checking && <RunShot runId={lastRun.id} />}
            <ul>
              {[...lastRun.log].reverse().map((l, i) => (
                <li key={i}>{l}</li>
              ))}
            </ul>
          </>
        )}
      </aside>
    </div>
  );
}

export default function AgentsApp({ me }: { me: User }) {
  const [tab, setTab] = useState<"mine" | "structure" | "returns" | "assembly">("mine");
  const [openSupplier, setOpenSupplier] = useState<number | null>(null);
  return (
    <div className="ag-shell">
      <div className="chips tabs">
        <button className={`chip${tab === "mine" ? " on" : ""}`} onClick={() => setTab("mine")}>{me.role === "owner" ? "Все агенты" : "Мои агенты"}</button>
        <button className={`chip${tab === "structure" ? " on" : ""}`} onClick={() => setTab("structure")}>Структура</button>
        <button className={`chip${tab === "assembly" ? " on" : ""}`} onClick={() => setTab("assembly")}>Сборка возвратов</button>
        <button className={`chip${tab === "returns" ? " on" : ""}`} onClick={() => setTab("returns")}>Возвраты поставщикам</button>
      </div>
      <div className="ag-body">{tab === "mine" ? <AgentList me={me} /> : tab === "structure" ? <AgentStructure /> : tab === "assembly" ? <AssemblyPanel onOpen={(id) => { setOpenSupplier(id); setTab("returns"); }} /> : <ReturnsPanel me={me} focusSupplier={openSupplier} />}</div>
    </div>
  );
}
