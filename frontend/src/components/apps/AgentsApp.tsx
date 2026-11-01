import { useEffect, useRef, useState } from "react";

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

const SUPPLIERS = [
  { id: "s1", name: "Поставщик А", site: "lk.supplier-a.example", items: 14, note: "Первый запуск: под вашим контролем" },
  { id: "s2", name: "Поставщик Б", site: "partner.supplier-b.example", items: 6, note: "Ждёт успешного первого запуска" },
  { id: "s3", name: "Поставщик В", site: "b2b.supplier-v.example", items: 0, note: "Нет возвратов в очереди" },
];

const TEMPLATE: Omit<Step, "state">[] = [
  { title: "Войти в личный кабинет", detail: "Открываю сайт поставщика, использую сохранённый доступ сотрудника" },
  { title: "Найти раздел возвратов", detail: "Перехожу в «Возвраты» → «Новая заявка»" },
  { title: "Заполнить позиции из таблицы", detail: "Артикул, количество и причина берутся из таблицы возвратов" },
  { title: "Проверить заявку", detail: "Сверяю сумму и количество с таблицей перед отправкой" },
  { title: "Отправить заявку поставщику", detail: "Необратимое действие: заявку нельзя отозвать", needsConfirm: true },
  { title: "Записать номер заявки в таблицу", detail: "Номер и статус вношу в таблицу возвратов" },
];

const stamp = () => new Date().toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const fresh = (): Step[] => TEMPLATE.map((s) => ({ ...s, state: "wait" }));
const ICON: Record<StepState, string> = { wait: "○", run: "◐", confirm: "!", done: "✓", skip: "–" };

/** Макет экрана агента по возвратам. Шаги имитируются на странице: реальный запуск подключим после доступа к кабинету поставщика. */
export default function AgentsApp() {
  const [supplier, setSupplier] = useState(SUPPLIERS[0].id);
  const [steps, setSteps] = useState<Step[]>(fresh);
  const [log, setLog] = useState<LogLine[]>([]);
  const [running, setRunning] = useState(false);
  const [finished, setFinished] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  const current = SUPPLIERS.find((s) => s.id === supplier)!;
  const awaiting = steps.findIndex((s) => s.state === "confirm");

  const confirmRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (awaiting >= 0) confirmRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [awaiting]);

  const add = (text: string) => setLog((l) => [{ at: stamp(), text }, ...l]);
  useEffect(() => () => window.clearTimeout(timer.current), []);

  function reset(id: string) {
    window.clearTimeout(timer.current);
    setSupplier(id);
    setSteps(fresh());
    setLog([]);
    setRunning(false);
    setFinished(false);
  }

  function advance(from: number, list: Step[]) {
    if (from >= list.length) {
      setRunning(false);
      setFinished(true);
      add("Готово: заявка отправлена, номер записан в таблицу");
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
    const list = fresh();
    setSteps(list);
    setLog([]);
    setFinished(false);
    setRunning(true);
    add(`Запуск для «${current.name}»: ${current.items} позиций`);
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

  return (
    <div className="agents">
      <aside className="ag-left">
        <h3>Возвраты поставщикам</h3>
        <p className="muted small">Агент работает в личных кабинетах с доступами сотрудника и просит подтверждения перед необратимым.</p>
        <ul>
          {SUPPLIERS.map((s) => (
            <li key={s.id}>
              <button className={s.id === supplier ? "on" : ""} onClick={() => reset(s.id)} disabled={running}>
                <strong>{s.name}</strong>
                <span className="muted small">{s.items ? `${s.items} позиций к возврату` : "нет позиций"}</span>
              </button>
            </li>
          ))}
        </ul>
        <p className="ag-demo">Макет: шаги показаны для примера, данные демонстрационные.</p>
      </aside>

      <section className="ag-main">
        <header>
          <div>
            <h2>{current.name}</h2>
            <p className="muted small">{current.site} · {current.note}</p>
          </div>
          {running ? (
            <button className="danger" onClick={stop}>Остановить</button>
          ) : (
            <button className="primary" onClick={start} disabled={!current.items}>{finished ? "Запустить снова" : "Запустить"}</button>
          )}
        </header>

        {!running && !finished && steps.every((s) => s.state === "wait") && (
          <p className="ag-hint">
            {current.items
              ? "Нажмите «Запустить». Агент пройдёт шаги ниже и остановится перед отправкой заявки, чтобы вы её подтвердили."
              : "Для этого поставщика нет возвратов. Добавьте позиции в таблицу возвратов."}
          </p>
        )}

        <ol className="ag-steps">
          {steps.map((s, i) => (
            <li key={i} className={`st-${s.state}`}>
              <span className="ag-ic">{ICON[s.state]}</span>
              <div>
                <strong>{s.title}</strong>
                <p className="muted small">{s.detail}</p>
                {s.state === "confirm" && (
                  <div className="ag-confirm" role="alert" ref={confirmRef}>
                    <p>Отправить заявку на {current.items} позиций поставщику «{current.name}»? Отменить это нельзя.</p>
                    <button className="primary" onClick={() => confirm(true)}>Подтвердить и отправить</button>
                    <button onClick={() => confirm(false)}>Отклонить</button>
                  </div>
                )}
              </div>
            </li>
          ))}
        </ol>
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
