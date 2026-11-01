import { CSSProperties, PointerEvent, ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { applyTheme, getTheme, Theme } from "../theme";
import type { IntegrationsResponse, User } from "../types";
import TasksApp from "./apps/TasksApp";
import FilesApp from "./apps/FilesApp";
import EmployeesApp from "./apps/EmployeesApp";
import IntegrationsApp from "./apps/IntegrationsApp";
import Placeholder from "./apps/Placeholder";
import Chat from "./Chat";
import CalendarWidget from "./widgets/CalendarWidget";
import IntegrationsWidget from "./widgets/IntegrationsWidget";
import Wallpaper from "./Wallpaper";
import Window, { WindowState } from "./Window";

interface AppDef {
  key: string;
  title: string;
  icon: string;
  colors: [string, string];
  stage?: number;
}

const APPS: AppDef[] = [
  { key: "gen", title: "Ген. директор", icon: "🏛️", colors: ["#f5b84a", "#e8793a"], stage: 3 },
  { key: "tech", title: "Тех. директор", icon: "🛠️", colors: ["#7aa7ff", "#4f5be0"], stage: 3 },
  { key: "ops", title: "Опер. директор", icon: "📦", colors: ["#6fd3a0", "#2f9e78"], stage: 3 },
  { key: "fin", title: "Фин. директор", icon: "💰", colors: ["#f2d36b", "#c79a1e"], stage: 3 },
  { key: "tasks", title: "Задачи", icon: "✅", colors: ["#8fd47a", "#3f9d4f"], stage: 1 },
  { key: "strategy", title: "Стратег. задачи", icon: "🎯", colors: ["#ff9a8a", "#e0514a"], stage: 4 },
  { key: "files", title: "Файлы", icon: "📁", colors: ["#ffd27a", "#f0a030"], stage: 3 },
  { key: "agents", title: "ИИ-агенты", icon: "🤖", colors: ["#b69bff", "#7a52e0"], stage: 2 },
  { key: "meetings", title: "Встречи", icon: "🎙️", colors: ["#7fd6e8", "#2f9bb8"], stage: 5 },
  { key: "integrations", title: "Интеграции", icon: "🔌", colors: ["#ff9fc4", "#d6478a"] },
  { key: "employees", title: "Сотрудники", icon: "👥", colors: ["#a9b4c8", "#667189"] },
];

const MIN_CHAT = 300;

function renderApp(app: AppDef, user: User): ReactNode {
  if (app.key === "integrations") return <IntegrationsApp />;
  if (app.key === "tasks") return <TasksApp />;
  if (app.key === "files") return <FilesApp />;
  if (app.key === "employees") return <EmployeesApp me={user} />;
  return <Placeholder title={app.title} stage={app.stage ?? 0} />;
}

export default function Desktop({ user, onLogout }: { user: User; onLogout: () => void }) {
  const [windows, setWindows] = useState<WindowState[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [chatWidth, setChatWidth] = useState<number>(() => Math.round(window.innerWidth * 0.4));
  const [chatOpen, setChatOpen] = useState(true);
  const [integ, setInteg] = useState<IntegrationsResponse | null>(null);
  const [theme, setThemeState] = useState<Theme>(getTheme);
  const [menuOpen, setMenuOpen] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState("");
  const zCounter = useRef(1);
  const resizing = useRef(false);

  const problems = integ ? integ.items.filter((i) => i.status === "error" || i.status === "warning").length : null;
  const working = integ ? integ.items.filter((i) => i.status === "ok").length : 0;

  // Сводка по интеграциям для виджета и верхней панели: обновляется раз в минуту.
  useEffect(() => {
    let alive = true;
    const load = () =>
      api<IntegrationsResponse>("/integrations")
        .then((r) => alive && setInteg(r))
        .catch(() => {});
    void load();
    const timer = setInterval(load, 60_000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, []);

  // Закрытие меню по клику снаружи и открытие поиска по Ctrl+K.
  useEffect(() => {
    const onDown = (e: Event) => {
      if (!(e.target as HTMLElement).closest(".menu")) setMenuOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setSearchOpen(true);
      }
    };
    document.addEventListener("pointerdown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, []);

  function toggleTheme() {
    const next: Theme = theme === "day" ? "night" : "day";
    applyTheme(next);
    setThemeState(next);
  }

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
      x: 40 + n * 28,
      y: 40 + n * 28,
      w: 760,
      h: 500,
      z: zCounter.current,
      minimized: false,
      maximized: false,
    };
    setWindows((ws) => [...ws, win]);
    setActiveId(win.id);
  }

  const openByKey = (key: string) => {
    const app = APPS.find((a) => a.key === key);
    if (app) open(app);
  };

  const patch = (id: string, change: Partial<WindowState>) =>
    setWindows((ws) => ws.map((w) => (w.id === id ? { ...w, ...change } : w)));

  function close(id: string) {
    setWindows((ws) => ws.filter((w) => w.id !== id));
    setActiveId((cur) => (cur === id ? null : cur));
  }

  function dockClick(app: AppDef) {
    const win = windows.find((w) => w.appKey === app.key);
    if (win && win.id === activeId && !win.minimized) {
      patch(win.id, { minimized: true });
      setActiveId(null);
    } else {
      open(app);
    }
  }

  function startResize(e: PointerEvent<HTMLDivElement>) {
    resizing.current = true;
    e.currentTarget.setPointerCapture(e.pointerId);
  }
  function doResize(e: PointerEvent<HTMLDivElement>) {
    if (!resizing.current) return;
    setChatWidth(Math.min(Math.max(e.clientX - 12, MIN_CHAT), window.innerWidth - 360));
  }

  const found = useMemo(() => {
    const q = query.trim().toLowerCase();
    return q ? APPS.filter((a) => a.title.toLowerCase().includes(q)) : APPS;
  }, [query]);

  function pick(app: AppDef) {
    open(app);
    setSearchOpen(false);
    setQuery("");
  }

  return (
    <div className="shell">
      {chatOpen && (
        <aside className="chat-panel" style={{ width: chatWidth }}>
          <header>
            <strong>Ассистент</strong>
            <button className="ghost" title="Свернуть чат" onClick={() => setChatOpen(false)}>
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

      <div className="main-col">
        <nav className="topbar">
          <div className="topbar-left">
            {!chatOpen && (
              <button className="pill" onClick={() => setChatOpen(true)}>
                ▶ Чат
              </button>
            )}
            <div className="menu">
              <button className="pill" onClick={() => setMenuOpen((v) => !v)}>
                Вид ▾
              </button>
              {menuOpen && (
                <div className="dropdown">
                  <button
                    onClick={() => {
                      setWindows((ws) => ws.map((w) => ({ ...w, minimized: true })));
                      setActiveId(null);
                      setMenuOpen(false);
                    }}
                  >
                    Показать рабочий стол
                  </button>
                  <button
                    disabled={windows.length === 0}
                    onClick={() => {
                      setWindows((ws) => ws.map((w) => ({ ...w, minimized: false })));
                      setMenuOpen(false);
                    }}
                  >
                    Показать все окна
                  </button>
                  <button
                    disabled={windows.length === 0}
                    onClick={() => {
                      setWindows([]);
                      setActiveId(null);
                      setMenuOpen(false);
                    }}
                  >
                    Закрыть все окна
                  </button>
                </div>
              )}
            </div>
            <button className="pill" onClick={() => setSearchOpen(true)}>
              Поиск <kbd>Ctrl K</kbd>
            </button>
          </div>
          <div className="topbar-right">
            {problems !== null && (
              <button
                className={`pill health ${problems > 0 ? "bad" : working > 0 ? "good" : ""}`}
                title="Состояние интеграций"
                onClick={() => openByKey("integrations")}
              >
                {problems > 0
                  ? `Интеграции: проблем ${problems}`
                  : working > 0
                    ? "Интеграции в порядке"
                    : "Интеграции не подключены"}
              </button>
            )}
            <button className="pill" onClick={toggleTheme} title="Сменить тему">
              {theme === "day" ? "🌙 Ночь" : "☀️ День"}
            </button>
            <span className="who">{user.full_name || user.email}</span>
            <button className="pill" onClick={onLogout}>
              Выйти
            </button>
          </div>
        </nav>

        <main className="desk">
          <Wallpaper theme={theme} />

          <div className="widgets">
            <IntegrationsWidget data={integ} onOpen={() => openByKey("integrations")} />
            <CalendarWidget />
          </div>

          <div className="windows">
            {windows.map((w) => {
              const app = APPS.find((a) => a.key === w.appKey)!;
              return (
                <Window
                  key={w.id}
                  win={w}
                  active={w.id === activeId}
                  icon={app.icon}
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

          {searchOpen && (
            <div className="search-overlay" onPointerDown={() => setSearchOpen(false)}>
              <div className="search-box" onPointerDown={(e) => e.stopPropagation()}>
                <input
                  autoFocus
                  placeholder="Разделы системы…"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Escape") setSearchOpen(false);
                    if (e.key === "Enter" && found[0]) pick(found[0]);
                  }}
                />
                <p className="muted">Пока ищем по разделам. Поиск по файлам, задачам и сообщениям появится вместе с ними.</p>
                <div className="search-results">
                  {found.map((a) => (
                    <button key={a.key} onClick={() => pick(a)}>
                      <span className="tile small" style={{ "--a": a.colors[0], "--b": a.colors[1] } as CSSProperties}>
                        {a.icon}
                      </span>
                      {a.title}
                    </button>
                  ))}
                  {found.length === 0 && <p className="muted">Ничего не найдено</p>}
                </div>
              </div>
            </div>
          )}

          <div className="dock">
            {APPS.map((app) => {
              const win = windows.find((w) => w.appKey === app.key);
              return (
                <button
                  key={app.key}
                  className={`dock-item${win ? " running" : ""}`}
                  title={app.title}
                  onClick={() => dockClick(app)}
                >
                  <span className="tile" style={{ "--a": app.colors[0], "--b": app.colors[1] } as CSSProperties}>
                    {app.icon}
                  </span>
                  <span className="dock-label">{app.title}</span>
                </button>
              );
            })}
          </div>
        </main>
      </div>
    </div>
  );
}
