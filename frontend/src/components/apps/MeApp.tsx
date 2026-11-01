import { useEffect, useMemo, useState } from "react";
import { api } from "../../api";
import type { Board, User } from "../../types";
import ConnectionsPanel from "./ConnectionsPanel";

type Tab = "overview" | "connections" | "profile";

export default function MeApp({ me }: { me: User }) {
  const [tab, setTab] = useState<Tab>("overview");
  const [board, setBoard] = useState<Board | null>(null);
  const name = me.full_name || me.email;

  useEffect(() => {
    api<Board>("/board").then(setBoard).catch(() => {});
  }, []);

  const mine = useMemo(() => {
    const all = board?.columns.flatMap((c) => c.cards).filter((c) => c.assignee_id === me.id && c.status !== "done") ?? [];
    return { all, late: all.filter((c) => c.overdue) };
  }, [board, me.id]);

  return (
    <div className="app-pad me">
      <div className="me-head">
        <span className="me-ava">{name.trim().slice(0, 2).toUpperCase()}</span>
        <div>
          <h2>{name}</h2>
          <p className="muted">{[me.position, me.department].filter(Boolean).join(" · ") || (me.role === "owner" ? "Владелец" : "Сотрудник")}</p>
        </div>
      </div>
      <div className="chips tabs">
        <button className={`chip${tab === "overview" ? " on" : ""}`} onClick={() => setTab("overview")}>Обзор</button>
        <button className={`chip${tab === "connections" ? " on" : ""}`} onClick={() => setTab("connections")}>Мои подключения</button>
        <button className={`chip${tab === "profile" ? " on" : ""}`} onClick={() => setTab("profile")}>Профиль</button>
      </div>

      {tab === "overview" && (
        <>
          <div className="me-stats">
            <div><b>{board ? mine.all.length : "…"}</b><span>моих открытых задач</span></div>
            <div className={mine.late.length ? "bad" : ""}><b>{board ? mine.late.length : "…"}</b><span>просрочено</span></div>
            <div><b>0</b><span>моих ИИ-агентов</span></div>
          </div>
          <h4>Что сделать сейчас</h4>
          {mine.all.length === 0 ? (
            <p className="muted">Открытых задач нет. Новую можно создать в разделе «Задачи» клавишей N.</p>
          ) : (
            <ul className="me-list">
              {[...mine.all].sort((a, b) => Number(b.overdue) - Number(a.overdue)).slice(0, 6).map((c) => (
                <li key={c.id} className={c.overdue ? "late" : ""}>
                  <span>{c.title}</span>
                  <span className="muted small">{c.overdue ? "просрочена" : c.deadline ? new Date(c.deadline).toLocaleDateString("ru-RU") : "без срока"}</span>
                </li>
              ))}
            </ul>
          )}
          <h4>Мои агенты</h4>
          <p className="muted">Здесь будут агенты, которых вы создали: для браузера и для Windows. Они работают только с вашими доступами и спрашивают подтверждение перед необратимым.</p>
        </>
      )}

      {tab === "connections" && <ConnectionsPanel />}

      {tab === "profile" && (
        <dl className="me-dl">
          <dt>Почта</dt><dd>{me.email}</dd>
          <dt>Роль</dt><dd>{me.role === "owner" ? "Владелец" : "Сотрудник"}</dd>
          <dt>Отдел</dt><dd>{me.department || "—"}</dd>
          <dt>Должность</dt><dd>{me.position || "—"}</dd>
          <dt>Id в Битрикс24</dt><dd>{me.bitrix_user_id ?? "не указан: задачи из Битрикс24 не подтянутся"}</dd>
          <dt>Применять изменения системы</dt><dd>{me.can_apply_changes ? "разрешено" : "нет права"}</dd>
        </dl>
      )}
    </div>
  );
}
