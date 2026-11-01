import { useState } from "react";

const MONTHS = [
  "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
  "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
];
const WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];

export default function CalendarWidget() {
  const today = new Date();
  const [cursor, setCursor] = useState(() => new Date(today.getFullYear(), today.getMonth(), 1));

  const offset = (cursor.getDay() + 6) % 7; // неделя начинается с понедельника
  const daysInMonth = new Date(cursor.getFullYear(), cursor.getMonth() + 1, 0).getDate();
  const cells: (number | null)[] = [
    ...Array.from({ length: offset }, () => null),
    ...Array.from({ length: daysInMonth }, (_, i) => i + 1),
  ];
  const isToday = (day: number) =>
    day === today.getDate() &&
    cursor.getMonth() === today.getMonth() &&
    cursor.getFullYear() === today.getFullYear();
  const shift = (delta: number) => setCursor((c) => new Date(c.getFullYear(), c.getMonth() + delta, 1));

  return (
    <div className="widget calendar">
      <div className="calendar-grid">
        <div className="calendar-head">
          <span className="widget-title">
            {MONTHS[cursor.getMonth()]} {cursor.getFullYear()}
          </span>
          <span>
            <button className="ghost" onClick={() => shift(-1)} aria-label="Предыдущий месяц">
              ‹
            </button>
            <button className="ghost" onClick={() => shift(1)} aria-label="Следующий месяц">
              ›
            </button>
          </span>
        </div>
        <div className="days">
          {WEEKDAYS.map((d) => (
            <span key={d} className="weekday">
              {d}
            </span>
          ))}
          {cells.map((day, i) => (
            <span key={i} className={day && isToday(day) ? "day today" : "day"}>
              {day}
            </span>
          ))}
        </div>
      </div>
      <div className="calendar-today">
        <div className="widget-title">Сегодня</div>
        <p className="muted">Встреч и запусков агентов пока нет. Календарь появится вместе с разделом «Встречи».</p>
      </div>
    </div>
  );
}
