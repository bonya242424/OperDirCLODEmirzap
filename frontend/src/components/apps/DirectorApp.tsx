import { FormEvent, useCallback, useEffect, useState } from "react";
import { api, formatAgo } from "../../api";
import type { Metric, MetricValue } from "../../types";

const STATUS_TEXT: Record<Metric["status"], string> = { ok: "В норме", warning: "Внимание", bad: "Плохо", no_data: "Нет данных" };
const fmt = (n: number | null): string => (n === null ? "—" : new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 }).format(n));
const today = (): string => new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);

function Spark({ values, status }: { values: number[]; status: Metric["status"] }) {
  if (values.length < 2) return <div className="spark empty">мало данных для графика</div>;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * 100},${28 - ((v - min) / span) * 24}`);
  return (
    <svg className={`spark ${status}`} viewBox="0 0 100 32" preserveAspectRatio="none" aria-hidden="true">
      <polyline points={pts.join(" ")} fill="none" strokeWidth="2" vectorEffect="non-scaling-stroke" strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={pts[pts.length - 1].split(",")[0]} cy={pts[pts.length - 1].split(",")[1]} r="2.6" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

interface Form {
  id: number | null;
  name: string;
  unit: string;
  description: string;
  better: "up" | "down";
  warn: string;
  bad: string;
  days: string;
}

const EMPTY: Form = { id: null, name: "", unit: "", description: "", better: "up", warn: "", bad: "", days: "7" };

export default function DirectorApp({ direction, title }: { direction: string; title: string }) {
  const [metrics, setMetrics] = useState<Metric[] | null>(null);
  const [error, setError] = useState("");
  const [form, setForm] = useState<Form | null>(null);
  const [open, setOpen] = useState<Metric | null>(null);
  const [values, setValues] = useState<MetricValue[]>([]);
  const [period, setPeriod] = useState(today());
  const [value, setValue] = useState("");
  const [note, setNote] = useState("");

  const fail = (e: unknown) => setError(e instanceof Error ? e.message : "Ошибка");

  const load = useCallback(() => {
    api<Metric[]>(`/metrics?direction=${direction}`)
      .then((m) => {
        setMetrics(m);
        setError("");
        setOpen((cur) => (cur ? (m.find((x) => x.id === cur.id) ?? null) : null));
      })
      .catch(fail);
  }, [direction]);

  useEffect(load, [load]);

  useEffect(() => {
    if (open) api<MetricValue[]>(`/metrics/${open.id}/values`).then(setValues).catch(fail);
  }, [open]);

  const num = (s: string): number | null => (s.trim() === "" ? null : Number(s.replace(",", ".")));

  async function saveMetric(e: FormEvent) {
    e.preventDefault();
    if (!form) return;
    const warn = num(form.warn);
    const bad = num(form.bad);
    if ((warn !== null && Number.isNaN(warn)) || (bad !== null && Number.isNaN(bad))) return setError("Пороги должны быть числами");
    const body = {
      name: form.name,
      unit: form.unit,
      description: form.description,
      better: form.better,
      warn_threshold: warn,
      bad_threshold: bad,
      expected_days: Number(form.days) || 7,
    };
    try {
      if (form.id === null) await api("/metrics", { method: "POST", body: { ...body, direction } });
      else await api(`/metrics/${form.id}`, { method: "PATCH", body: { ...body, clear_thresholds: warn === null && bad === null } });
      setForm(null);
      load();
    } catch (err) {
      fail(err);
    }
  }

  async function addValue(e: FormEvent) {
    e.preventDefault();
    if (!open) return;
    const v = num(value);
    if (v === null || Number.isNaN(v)) return setError("Введите число");
    try {
      await api(`/metrics/${open.id}/values`, { method: "POST", body: { period, value: v, note } });
      setValue("");
      setNote("");
      load();
      api<MetricValue[]>(`/metrics/${open.id}/values`).then(setValues);
    } catch (err) {
      fail(err);
    }
  }

  async function archive(m: Metric) {
    if (!window.confirm(`Убрать показатель «${m.name}»? Значения сохранятся в базе.`)) return;
    try {
      await api(`/metrics/${m.id}`, { method: "DELETE" });
      setOpen(null);
      load();
    } catch (err) {
      fail(err);
    }
  }

  const RANK: Record<Metric["status"], number> = { bad: 0, warning: 1, no_data: 2, ok: 3 };
  const sorted = metrics ? [...metrics].sort((a, b) => RANK[a.status] - RANK[b.status] || a.name.localeCompare(b.name, "ru")) : null;
  const count = (s: Metric["status"]) => metrics?.filter((m) => m.status === s).length ?? 0;
  const stale = metrics?.filter((m) => m.stale).length ?? 0;

  return (
    <div className={`director${open ? " has-drawer" : ""}`}>
      <div className="kanban-bar">
        <div>
          <h2>{title}</h2>
          {metrics && metrics.length > 0 && (
            <p className="muted">
              <span className="good">{count("ok")} в норме</span> · <span className="warnc">{count("warning")} внимание</span> ·{" "}
              <span className="late">{count("bad")} плохо</span>
              {stale > 0 && <> · {stale} не обновлялись</>}
            </p>
          )}
        </div>
        <button className="primary" onClick={() => setForm(EMPTY)}>+ Показатель</button>
      </div>
      {error && <div className="error">{error}</div>}
      {metrics && metrics.length === 0 && (
        <div className="empty-state">
          <div className="empty-icon">📈</div>
          <strong>Здесь пока нет показателей</strong>
          <p className="muted">
            Добавьте то, за чем следите: например «Конверсия в заказ, %» или «Возвраты, %». Задайте жёлтый и красный пороги,
            и плитка сама покажет, когда пора реагировать. Значения можно вносить вручную, позже их будут подтягивать выгрузки.
          </p>
          <button className="primary" onClick={() => setForm(EMPTY)}>+ Добавить первый показатель</button>
        </div>
      )}
      {!metrics && !error && (
        <div className="tiles" aria-busy="true">
          {[0, 1, 2].map((i) => <div key={i} className="tile-m skeleton" />)}
        </div>
      )}
      <div className="tiles">
        {sorted?.map((m) => (
          <button key={m.id} className={`tile-m ${m.status}${open?.id === m.id ? " sel" : ""}`} onClick={() => setOpen(m)}>
            <div className="tile-top">
              <span className="tile-name">{m.name}</span>
              <span className={`pillst ${m.status}`}>{STATUS_TEXT[m.status]}</span>
            </div>
            <div className="tile-val">
              {fmt(m.latest)}
              <small>{m.unit}</small>
              {m.change !== null && m.change !== 0 && (
                <span className={`delta ${(m.change > 0) === (m.better === "up") ? "pos" : "neg"}`} title="Изменение к предыдущему значению">
                  {m.change > 0 ? "▲" : "▼"} {fmt(Math.abs(m.change))} <small>к пред.</small>
                </span>
              )}
            </div>
            <Spark values={m.history} status={m.status} />
            <div className="tile-foot muted">
              {m.latest_period ? (m.stale ? <span className="warnc">устарело: {formatAgo(m.latest_period)}</span> : `за ${new Date(m.latest_period).toLocaleDateString("ru-RU")}`) : "значений ещё нет"}
            </div>
          </button>
        ))}
      </div>

      {open && (
        <aside className="drawer">
          <div className="drawer-head">
            <div>
              <h3>{open.name}</h3>
              {open.description && <p className="muted">{open.description}</p>}
            </div>
            <button className="ghost" onClick={() => setOpen(null)} aria-label="Закрыть">✕</button>
          </div>
          <p className="muted">
            {open.better === "up" ? "Чем больше, тем лучше" : "Чем меньше, тем лучше"}.{" "}
            {open.warn_threshold !== null && <>Внимание: {open.better === "up" ? "≤" : "≥"} {fmt(open.warn_threshold)}. </>}
            {open.bad_threshold !== null && <>Плохо: {open.better === "up" ? "≤" : "≥"} {fmt(open.bad_threshold)}. </>}
            Ждём обновление раз в {open.expected_days} дн.
          </p>
          <form className="value-form" onSubmit={addValue}>
            <input type="date" value={period} max={today()} onChange={(e) => setPeriod(e.target.value)} />
            <input inputMode="decimal" placeholder={`Значение${open.unit ? `, ${open.unit}` : ""}`} value={value} onChange={(e) => setValue(e.target.value)} required />
            <input placeholder="Комментарий (необязательно)" value={note} onChange={(e) => setNote(e.target.value)} />
            <button className="primary" type="submit">Записать значение</button>
          </form>
          <h4>История</h4>
          <ul className="history">
            {values.map((v) => (
              <li key={v.period}>
                <span>{new Date(v.period).toLocaleDateString("ru-RU")}</span>
                <b>{fmt(v.value)} {open.unit}</b>
                <span className="muted">{v.note}</span>
              </li>
            ))}
            {values.length === 0 && <li className="muted">Значений ещё нет</li>}
          </ul>
          <div className="modal-actions">
            <button className="ghost danger" onClick={() => archive(open)}>Убрать</button>
            <span className="grow" />
            <button
              onClick={() =>
                setForm({
                  id: open.id, name: open.name, unit: open.unit, description: open.description, better: open.better,
                  warn: open.warn_threshold?.toString() ?? "", bad: open.bad_threshold?.toString() ?? "", days: String(open.expected_days),
                })
              }
            >
              Настроить
            </button>
          </div>
        </aside>
      )}

      {form && (
        <div className="modal-bg" onPointerDown={() => setForm(null)}>
          <form className="modal" onSubmit={saveMetric} onPointerDown={(e) => e.stopPropagation()}>
            <h3>{form.id === null ? "Новый показатель" : "Настройка показателя"}</h3>
            <input autoFocus required placeholder="Название, например «Конверсия в заказ»" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
            <div className="modal-grid">
              <label>
                Единицы
                <input placeholder="%, шт, ₽" value={form.unit} onChange={(e) => setForm({ ...form, unit: e.target.value })} />
              </label>
              <label>
                Что лучше
                <select value={form.better} onChange={(e) => setForm({ ...form, better: e.target.value as "up" | "down" })}>
                  <option value="up">Чем больше, тем лучше</option>
                  <option value="down">Чем меньше, тем лучше</option>
                </select>
              </label>
              <label>
                Порог «внимание»
                <input inputMode="decimal" value={form.warn} onChange={(e) => setForm({ ...form, warn: e.target.value })} />
              </label>
              <label>
                Порог «плохо»
                <input inputMode="decimal" value={form.bad} onChange={(e) => setForm({ ...form, bad: e.target.value })} />
              </label>
              <label>
                Ждать обновление раз в (дней)
                <input inputMode="numeric" value={form.days} onChange={(e) => setForm({ ...form, days: e.target.value })} />
              </label>
            </div>
            <textarea rows={2} placeholder="Описание: как считается, откуда данные" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} />
            <p className="muted">
              {form.better === "up" ? "Порог «плохо» должен быть ниже порога «внимание»." : "Порог «плохо» должен быть выше порога «внимание»."}
            </p>
            <div className="modal-actions">
              <span className="grow" />
              <button type="button" onClick={() => setForm(null)}>Отмена</button>
              <button type="submit" className="primary">Сохранить</button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}
