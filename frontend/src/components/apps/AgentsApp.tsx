import { ChangeEvent, FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../api";
import type { User } from "../../types";
import AgentList from "./AgentList";
import AgentStructure from "./AgentStructure";

type StepState = "wait" | "run" | "confirm" | "done" | "skip";
interface Step {
  title: string;
  detail: string;
  needsConfirm?: boolean;
  state: StepState;
}
interface LogLine {
  at: string;
  text: string;
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
}
type SupplierForm = Omit<Supplier, "id" | "batches" | "positions"> & { id: number | null };
const EMPTY_SUP: SupplierForm = { id: null, name: "", portal_url: "", contact: "", rules: "", default_reason: "", notes: "" };

interface Summary {
  id: number;
  supplier_id: number | null;
  supplier: string;
  number: string;
  source: string;
  positions: number;
  qty: number;
  invoices: { invoice_no: string; positions: number; qty: number; date: string }[];
  warehouses: { warehouse: string; positions: number; qty: number }[];
  reasons: { reason: string; qty: number }[];
  status: Record<string, number>;
  problems: string[];
}

const TEMPLATE: Omit<Step, "state">[] = [
  { title: "Открыть «Оформление возврата»", detail: "Вход уже выполнен вами в браузере; агент пароли не вводит" },
  { title: "Найти счёт по номеру из таблицы", detail: "Ищу по номеру счёта; если период поиска короче даты счёта, расширяю его" },
  { title: "Выставить количество «К возврату»", detail: "Для каждой позиции счёта количество берётся из таблицы" },
  { title: "Заполнить окно возврата", detail: "Причина из таблицы; адрес и контактное лицо сайт подставляет сам; файлы не нужны" },
  { title: "Отправить заявку", detail: "Необратимое действие: заявку нельзя отозвать", needsConfirm: true },
  { title: "Записать номер заявки в таблицу", detail: "Номер заявки и статус вносятся в строки счёта" },
];

const stamp = () => new Date().toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const fresh = (): Step[] => TEMPLATE.map((s) => ({ ...s, state: "wait" }));
const ICON: Record<StepState, string> = { wait: "○", run: "◐", confirm: "!", done: "✓", skip: "–" };

/** Агент по возвратам. Таблица и саммари настоящие; шаги работы на сайте пока показаны для примера: запуск на сайте подключим после первой проверки. */
function ReturnsPanel() {
  const [suppliers, setSuppliers] = useState<Supplier[] | null>(null);
  const [supSel, setSupSel] = useState<number | null>(null);
  const [supForm, setSupForm] = useState<SupplierForm | null>(null);
  const [allBatches, setBatches] = useState<Summary[] | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [link, setLink] = useState("");
  const [busy, setBusy] = useState(false);
  const [steps, setSteps] = useState<Step[]>(fresh);
  const [log, setLog] = useState<LogLine[]>([]);
  const [running, setRunning] = useState(false);
  const [finished, setFinished] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  const confirmRef = useRef<HTMLDivElement>(null);
  const supplier = suppliers?.find((x) => x.id === supSel) ?? null;
  const batches = allBatches?.filter((b) => b.supplier_id === supSel) ?? null;
  const current = batches?.find((b) => b.id === selected) ?? null;
  const awaiting = steps.findIndex((s) => s.state === "confirm");

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
  useEffect(() => () => window.clearTimeout(timer.current), []);
  useEffect(() => {
    setSelected((cur) => (cur !== null && batches?.some((x) => x.id === cur) ? cur : batches?.[0]?.id ?? null));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [supSel, allBatches]);
  useEffect(() => {
    if (awaiting >= 0) confirmRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [awaiting]);

  const add = (text: string) => setLog((l) => [{ at: stamp(), text }, ...l]);

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

  function reset() {
    window.clearTimeout(timer.current);
    setSteps(fresh());
    setLog([]);
    setRunning(false);
    setFinished(false);
  }

  function advance(from: number, list: Step[]) {
    if (from >= list.length) {
      setRunning(false);
      setFinished(true);
      add("Готово (пример): номер заявки записан в таблицу");
      return;
    }
    const next = list.map((s, i) => (i === from ? { ...s, state: (s.needsConfirm ? "confirm" : "run") as StepState } : s));
    setSteps(next);
    if (next[from].needsConfirm) {
      add(`Жду подтверждения: «${next[from].title}»`);
      return;
    }
    add(next[from].title);
    timer.current = window.setTimeout(() => {
      const done = next.map((s, i) => (i === from ? { ...s, state: "done" as StepState } : s));
      setSteps(done);
      advance(from + 1, done);
    }, 1400);
  }

  function start() {
    if (!current) return;
    const list = fresh();
    setSteps(list);
    setLog([]);
    setFinished(false);
    setRunning(true);
    add(`Пример запуска для «${current.supplier}», сборка ${current.number}: счетов ${current.invoices.length}`);
    advance(0, list);
  }

  function stop() {
    window.clearTimeout(timer.current);
    setRunning(false);
    setSteps((s) => s.map((x) => (x.state === "run" || x.state === "confirm" ? { ...x, state: "wait" } : x)));
    add("Остановлено владельцем. Ничего необратимого не отправлено");
  }

  function confirm(yes: boolean) {
    if (awaiting < 0) return;
    if (!yes) {
      stop();
      add("Отправка отклонена");
      return;
    }
    add("Подтверждено владельцем");
    const done = steps.map((s, i) => (i === awaiting ? { ...s, state: "done" as StepState } : s));
    setSteps(done);
    advance(awaiting + 1, done);
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
                <span className="muted small">{x.batches} док. · {x.positions} поз.</span>
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
            {batches && batches.length > 0 && (
              <div className="chips">
                {batches.map((b) => (
                  <button key={b.id} className={`chip${b.id === selected ? " on" : ""}`} onClick={() => { setSelected(b.id); reset(); }} disabled={running}>
                    {b.number || `Документ ${b.id}`} · {b.positions} поз.
                  </button>
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
                {running ? (
                  <button className="danger" onClick={stop}>Остановить</button>
                ) : (
                  <button className="primary" onClick={start}>{finished ? "Показать снова" : "Показать запуск"}</button>
                )}
              </div>
            </header>

            <div className="ag-sum">
              <div><b>{current.positions}</b><span>позиций</span></div>
              <div><b>{current.qty}</b><span>штук</span></div>
              <div><b>{current.invoices.length}</b><span>счетов = заявок</span></div>
              <div><b>{done}</b><span>отправлено</span></div>
            </div>
            {current.problems.map((p) => (
              <p key={p} className="ag-problem">{p}</p>
            ))}

            <h4>По счетам: одна заявка на счёт</h4>
            <table className="ag-table">
              <thead><tr><th>Счёт</th><th>Дата</th><th>Позиций</th><th>Штук</th></tr></thead>
              <tbody>
                {current.invoices.map((i) => (
                  <tr key={i.invoice_no}><td>{i.invoice_no}</td><td>{i.date}</td><td>{i.positions}</td><td>{i.qty}</td></tr>
                ))}
              </tbody>
            </table>
            <p className="muted small">
              Причина: {current.reasons.map((r) => `${r.reason} (${r.qty} шт.)`).join(", ")}. Склады: {current.warehouses.map((w) => `${w.warehouse} ${w.qty}`).join(", ")}.
            </p>

            {(running || finished || steps.some((s) => s.state !== "wait")) && (
              <>
                <h4>Как пройдёт запуск (пример)</h4>
                <ol className="ag-steps">
                  {steps.map((s, i) => (
                    <li key={i} className={`st-${s.state}`}>
                      <span className="ag-ic">{ICON[s.state]}</span>
                      <div>
                        <strong>{s.title}</strong>
                        <p className="muted small">{s.detail}</p>
                        {s.state === "confirm" && (
                          <div className="ag-confirm" role="alert" ref={confirmRef}>
                            <p>Отправить заявку по счёту {current.invoices[0]?.invoice_no}? Отменить это нельзя.</p>
                            <button className="primary" onClick={() => confirm(true)}>Подтвердить и отправить</button>
                            <button onClick={() => confirm(false)}>Отклонить</button>
                          </div>
                        )}
                      </div>
                    </li>
                  ))}
                </ol>
              </>
            )}
          </>
        )}
      </section>

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
        {log.length === 0 ? (
          <p className="muted small">Здесь появится каждое действие агента со временем. Владелец видит журнал всех сотрудников.</p>
        ) : (
          <ul>
            {log.map((l, i) => (
              <li key={i}><span className="muted small">{l.at}</span> {l.text}</li>
            ))}
          </ul>
        )}
      </aside>
    </div>
  );
}

export default function AgentsApp({ me }: { me: User }) {
  const [tab, setTab] = useState<"mine" | "structure" | "returns">("mine");
  return (
    <div className="ag-shell">
      <div className="chips tabs">
        <button className={`chip${tab === "mine" ? " on" : ""}`} onClick={() => setTab("mine")}>{me.role === "owner" ? "Все агенты" : "Мои агенты"}</button>
        <button className={`chip${tab === "structure" ? " on" : ""}`} onClick={() => setTab("structure")}>Структура</button>
        <button className={`chip${tab === "returns" ? " on" : ""}`} onClick={() => setTab("returns")}>Возвраты поставщикам</button>
      </div>
      <div className="ag-body">{tab === "mine" ? <AgentList me={me} /> : tab === "structure" ? <AgentStructure /> : <ReturnsPanel />}</div>
    </div>
  );
}
