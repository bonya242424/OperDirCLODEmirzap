import { PointerEvent, ReactNode, useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { IntegrationsResponse, User } from "../types";
import EmployeesApp from "./apps/EmployeesApp";
import IntegrationsApp from "./apps/IntegrationsApp";
import Placeholder from "./apps/Placeholder";
import Chat from "./Chat";
import Window, { WindowState } from "./Window";

interface AppDef {
  key: string;
  title: string;
  icon: string;
  stage?: number;
}

const APPS: AppDef[] = [
  { key: "gen", title: "Ген. директор", icon: "🏛️", stage: 3 },
  { key: "tech", title: "Тех. директор", icon: "🛠️", stage: 3 },
  { key: "ops", title: "Опер. директор", icon: "📦", stage: 3 },
  { key: "fin", title: "Фин. директор", icon: "💰", stage: 3 },
  { key: "tasks", title: "Задачи", icon: "✅", stage: 1 },
  { key: "strategy", title: "Стратег. задачи", icon: "🎯", stage: 4 },
  { key: "files", title: "Файлы", icon: "📁", stage: 3 },
  { key: "agents", title: "ИИ-агенты", icon: "🤖", stage: 2 },
  { key: "meetings", title: "Встречи", icon: "🎙️", stage: 5 },
  { key: "integrations", title: "Интеграции", icon: "🔌" },
  { key: "employees", title: "Сотрудники", icon: "👥" },
];

const TASKBAR_HEIGHT = 44;
const MIN_CHAT = 300;

function renderApp(app: AppDef, user: User): ReactNode {
  if (app.key === "integrations") return <IntegrationsApp />;
  if (app.key === "employees") return <EmployeesApp me={user} />;
  return <Placeholder title={app.title} stage={app.stage ?? 0} />;
}

export default function Desktop({ user, onLogout }: { user: User; onLogout: () => void }) {
  const [windows, setWindows] = useState<WindowState[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [chatWidth, setChatWidth] = useState<number>(() => Math.round(window.innerWidth * 0.5));
  const [chatOpen, setChatOpen] = useState(true);
  const [problems, setProblems] = useState<number | null>(null);
  const zCounter = useRef(1);
  const resizing = useRef(false);

  // Сводка по интеграциям для панели задач: обновляется раз в минуту.
  useEffect(() => {
    let alive = true;
    const load = () =>
      api<IntegrationsResponse>("/integrations")
        .then((r) => alive && setProblems(r.items.filter((i) => i.status === "error" || i.status === "warning").length))
        .catch(() => {});
    void load();
    const timer = setInterval(load, 60_000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, []);

  function focus(id: string) {
    zCounter.current += 1;
    const z = zCounter.current;
    setActiveId(id);
    setWindows((ws) => ws.map((w) => (w.id === id ? { ...w, z, minimized: false } : w)));
  }

  function open(app: AppDef) {
    const existing = windows.find((w) => w.appKey === app.key);
    if (existing) return focus(existing.id);
    zCounter.current += 1;
    const n = windows.length;
    const win: WindowState = {
      id: `${app.key}-${Date.now()}`,
      appKey: app.key,
      title: app.title,
      x: 160 + n * 28,
      y: 40 + n * 28,
      w: 720,
      h: 480,
      z: zCounter.current,
      minimized: false,
      maximized: false,
    };
    setWindows((ws) => [...ws, win]);
    setActiveId(win.id);
  }

  const patch = (id: string, change: Partial<WindowState>) =>
    setWindows((ws) => ws.map((w) => (w.id === id ? { ...w, ...change } : w)));

  function close(id: string) {
    setWindows((ws) => ws.filter((w) => w.id !== id));
    setActiveId((cur) => (cur === id ? null : cur));
  }

  function startResize(e: PointerEvent<HTMLDivElement>) {
    resizing.current = true;
    e.currentTarget.setPointerCapture(e.pointerId);
  }
  function doResize(e: PointerEvent<HTMLDivElement>) {
    if (!resizing.current) return;
    setChatWidth(Math.min(Math.max(e.clientX, MIN_CHAT), window.innerWidth - 320));
  }

  return (
    <div className="shell">
      {chatOpen && (
        <aside className="chat-panel" style={{ width: chatWidth }}>
          <header>
            <strong>Ассистент</strong>
            <button title="Свернуть чат" onClick={() => setChatOpen(false)}>
              ◀
            </button>
          </header>
          <Chat />
          <div
            className="chat-resizer"
            onPointerDown={startResize}
            onPointerMove={doResize}
            onPointerUp={() => (resizing.current = false)}
          />
        </aside>
      )}

      <main className="desktop">
        {!chatOpen && (
          <button className="chat-reopen" onClick={() => setChatOpen(true)}>
            ▶ Чат
          </button>
        )}

        <div className="icons">
          {APPS.map((app) => (
            <button key={app.key} className="icon" onDoubleClick={() => open(app)} onClick={() => open(app)}>
              <span className="icon-glyph">{app.icon}</span>
              <span className="icon-label">{app.title}</span>
            </button>
          ))}
        </div>

        <div className="windows" style={{ bottom: TASKBAR_HEIGHT }}>
          {windows.map((w) => {
            const app = APPS.find((a) => a.key === w.appKey)!;
            return (
              <Window
                key={w.id}
                win={w}
                active={w.id === activeId}
                onFocus={() => focus(w.id)}
                onMove={(x, y) => patch(w.id, { x, y })}
                onClose={() => close(w.id)}
                onMinimize={() => {
                  patch(w.id, { minimized: true });
                  setActiveId((cur) => (cur === w.id ? null : cur));
                }}
                onToggleMaximize={() => patch(w.id, { maximized: !w.maximized })}
              >
                {renderApp(app, user)}
              </Window>
            );
          })}
        </div>

        <footer className="taskbar" style={{ height: TASKBAR_HEIGHT }}>
          <div className="taskbar-apps">
            {windows.map((w) => (
              <button
                key={w.id}
                className={w.id === activeId && !w.minimized ? "on" : ""}
                onClick={() => (w.id === activeId && !w.minimized ? patch(w.id, { minimized: true }) : focus(w.id))}
              >
                {w.title}
              </button>
            ))}
          </div>
          <div className="taskbar-right">
            {problems !== null && (
              <button
                className={`health ${problems > 0 ? "bad" : "good"}`}
                title="Состояние интеграций"
                onClick={() => open(APPS.find((a) => a.key === "integrations")!)}
              >
                {problems > 0 ? `Интеграции: проблем ${problems}` : "Интеграции в порядке"}
              </button>
            )}
            <span className="who">{user.full_name || user.email}</span>
            <button onClick={onLogout}>Выйти</button>
          </div>
        </footer>
      </main>
    </div>
  );
}
