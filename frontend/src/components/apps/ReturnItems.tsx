import { useState } from "react";

export interface ReturnItem {
  id: number; article: string; name: string; brand: string; qty: number;
  warehouse: string; invoice_date: string; invoice_no: string; reason: string;
  status: string; request_no: string; note: string;
}

const LABELS: Record<string, string> = {
  pending: "В очереди", checked: "Проверено, не отправлено", failed: "Есть проблема",
  submitted: "Возврат оформлен", submitting: "Отправляется", uncertain: "Требует сверки", skipped: "Пропущен",
};

export default function ReturnItems({ rows, canReconcile, disabled, onReconcile }: {
  rows: ReturnItem[]; canReconcile: boolean; disabled: boolean; onReconcile: (item: ReturnItem) => void;
}) {
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const needle = search.trim().toLocaleLowerCase("ru");
  const filtered = rows.filter((item) => (!status || item.status === status) &&
    (!needle || [item.article, item.brand, item.name, item.invoice_no].join(" ").toLocaleLowerCase("ru").includes(needle)));
  return (
    <section className="return-items" aria-label="Товары в сборке">
      <div className="return-items-heading"><h4>Товары в сборке</h4><span className="muted small">Показано {filtered.length} из {rows.length}</span></div>
      <div className="return-items-filters">
        <input aria-label="Найти товар" placeholder="Артикул, бренд, название или счёт" value={search} onChange={(e) => setSearch(e.target.value)} />
        <select aria-label="Статус товара" value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">Все статусы</option>
          {Object.entries(LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
        {(search || status) && <button className="ghost" onClick={() => { setSearch(""); setStatus(""); }}>Сбросить</button>}
      </div>
      <p className="muted small">Фильтры меняют только список. Запуск оформления обрабатывает всю сборку.</p>
      <div className="return-items-scroll" tabIndex={0} aria-label="Список товаров с прокруткой">
        <table className="ag-table return-items-table">
          <colgroup><col style={{ width: "30%" }} /><col style={{ width: "9%" }} /><col style={{ width: "14%" }} /><col style={{ width: "18%" }} /><col style={{ width: "29%" }} /></colgroup>
          <thead><tr><th>Товар</th><th>К возврату</th><th>Склад</th><th>Счёт</th><th>Результат</th></tr></thead>
          <tbody>{filtered.map((item) => (
            <tr key={item.id}>
              <td><strong className="return-article">{item.article}</strong><span className="return-brand muted small">{item.brand}</span><span className="return-item-name" title={item.name}>{item.name}</span></td>
              <td><strong>{item.qty}</strong><span className="muted small"> шт.</span></td>
              <td>{item.warehouse || "—"}</td>
              <td><span className="return-invoice">{item.invoice_no || "Без номера"}</span><span className="return-brand muted small">{item.invoice_date}</span></td>
              <td>
                <span className={`return-status inv-${item.status}`}>{LABELS[item.status] ?? item.status}</span>
                {item.request_no && <span className="return-brand small">Заявка {item.request_no}</span>}
                <details className="return-item-details"><summary>Подробности</summary>
                  {item.note && <p>{item.note}</p>}
                  <p><b>Наименование:</b> {item.name || "—"}</p>
                  <p><b>Причина возврата:</b> {item.reason || "Не указана"}</p>
                </details>
                {canReconcile && item.status === "uncertain" && <button className="ghost" disabled={disabled} onClick={() => onReconcile(item)}>Сверить отправку</button>}
              </td>
            </tr>
          ))}</tbody>
        </table>
        {!filtered.length && <p className="muted">{rows.length ? "По этим фильтрам товаров нет." : "В сборке нет товаров."}</p>}
      </div>
    </section>
  );
}
