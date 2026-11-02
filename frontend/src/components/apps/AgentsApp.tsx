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

interface Summary {
  id: number;
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
  const [batches, setBatches] = useState<Summary[] | null>(null);
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
  const current = batches?.find((b) => b.id === selected) ?? null;
  const awaiting = steps.findIndex((s) => s.state === "confirm");

  const load = useCallback(() => {
    api<Summary[]>("/returns/batches")
      .then((b) => {
        setBatches(b);
        setSelected((cur) => (cur !== null && b.some((x) => x.id === cur) ? cur : b[0]?.id ?? null));
      })
      .catch((e) => setError(e instanceof Error ? e.message : "Ошибка"));
  }, []);
  useEffect(load, [load]);
  useEffect(() => () => window.clearTimeout(timer.current), []);
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
    await send(() => api<Summary>("/returns/import", { method: "POST", body: fd }));
  }

  async function importLink(e: FormEvent) {
    e.preventDefault();
    if (!link.trim()) return;
    await send(() => api<Summary>("/returns/import-url", { method: "POST", body: { url: link.trim() } }));
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

  const done = current?.status.submitted ?? 0;

  return (
    <div className="agents">
      <aside className="ag-left">
        <h3>Возвраты поставщикам</h3>
        <p className="muted small">Загрузите таблицу со сборкой, и здесь появится саммари по поставщику.</p>
        <ul>
          {batches?.map((b) => (
            <li key={b.id}>
              <button className={b.id === selected ? "on" : ""} onClick={() => { setSelected(b.id); reset(); }} disabled={running}>
                <strong>{b.supplier}</strong>
                <span className="muted small">{b.number || "без номера"}</span>
                <span className="muted small">{b.positions} поз. · {b.qty} шт.</span>
              </button>
            </li>
          ))}
        </ul>
        {batches && batches.length === 0 && <p className="muted small">Сборок пока нет.</p>}
        <label className="ag-file">
          + Загрузить CSV
          <input type="file" accept=".csv,text/csv" onChange={upload} disabled={busy} hidden />
        </label>
        <form onSubmit={importLink} className="ag-link">
          <input placeholder="Ссылка на Google Таблицу" value={link} onChange={(e) => setLink(e.target.value)} />
          <button disabled={busy || !link.trim()}>Загрузить</button>
        </form>
        {error && <p className="error">{error}</p>}
      </aside>

      <section className="ag-main">
        {!current ? (
          <p className="ag-hint">Выберите сборку слева или загрузите таблицу. Нужны колонки: Товар, Кол. по документу, Номер счета, причина возврата. Строки «Поставщик» и «Сборка» сверху.</p>
        ) : (
          <>
            <header>
              <div>
                <h2>{current.supplier}</h2>
                <p className="muted small">Сборка {current.number || "без номера"} · источник: {current.source}</p>
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
