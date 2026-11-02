import ChangelogApp from "./apps/ChangelogApp";
import InstructionsApp from "./apps/InstructionsApp";
import RemoteApp from "./apps/RemoteApp";
import MeApp from "./apps/MeApp";
import AgentsApp from "./apps/AgentsApp";
import { CSSProperties, PointerEvent, ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { applyTheme, getTheme, Theme } from "../theme";
import type { IntegrationsResponse, User } from "../types";
import DirectorApp from "./apps/DirectorApp";
import TasksApp from "./apps/TasksApp";
import FilesApp from "./apps/FilesApp";
import EmployeesApp from "./apps/EmployeesApp";
import IntegrationsApp from "./apps/IntegrationsApp";
import Placeholder from "./apps/Placeholder";
import Chat from "./Chat";
import CalendarWidget from "./widgets/CalendarWidget";
import FocusWidget from "./widgets/FocusWidget";
import IntegrationsWidget from "./widgets/IntegrationsWidget";
import Wallpaper from "./Wallpaper";
import Window, { WindowState } from "./Window";

interface AppDef {
  key: string;
  title: string;
  icon: string;
  colors: [string, string];
  stage?: number;
  size?: [number, number];
}

const APPS: AppDef[] = [
  { key: "gen", title: "Ген. директор", icon: "🏛️", colors: ["#f5b84a", "#e8793a"], size: [1000, 640], stage: 3 },
  { key: "tech", title: "Тех. директор", icon: "🛠️", colors: ["#7aa7ff", "#4f5be0"], size: [1000, 640], stage: 3 },
  { key: "ops", title: "Опер. директор", icon: "📦", colors: ["#6fd3a0", "#2f9e78"], size: [1000, 640], stage: 3 },
  { key: "fin", title: "Фин. директор", icon: "💰", colors: ["#f2d36b", "#c79a1e"], size: [1000, 640], stage: 3 },
  { key: "tasks", title: "Задачи", icon: "✅", colors: ["#8fd47a", "#3f9d4f"], size: [1100, 660], stage: 1 },
  { key: "strategy", title: "Стратег. задачи", icon: "🎯", colors: ["#ff9a8a", "#e0514a"], stage: 4 },
  { key: "files", title: "Файлы", icon: "📁", colors: ["#ffd27a", "#f0a030"], size: [920, 580], stage: 3 },
  { key: "agents", title: "ИИ-агенты", icon: "🤖", colors: ["#b69bff", "#7a52e0"], size: [1060, 620], stage: 2 },
  { key: "calendar", title: "Календарь", icon: "📅", colors: ["#ffb199", "#ff6a5f"], stage: 5 },
  { key: "meetings", title: "Встречи", icon: "🎙️", colors: ["#7fd6e8", "#2f9bb8"], stage: 5 },
  { key: "integrations", title: "Интеграции", icon: "🔌", colors: ["#ff9fc4", "#d6478a"], size: [940, 620] },
  { key: "changes", title: "Изменения", icon: "🧩", colors: ["#c3b5ff", "#7c5ce6"], stage: 6 },
  { key: "remote", title: "Удалённые окна", icon: "🖥️", colors: ["#7fd6c2", "#2b8f7a"], size: [1180, 720] },
  { key: "instr1c", title: "Инструкции 1С", icon: "📘", colors: ["#8fb8ff", "#4a6fe0"], size: [1040, 640] },
  { key: "journal", title: "Журнал изменений", icon: "📝", colors: ["#9ad7c0", "#3a9c86"], size: [760, 600] },
  { key: "terminal", title: "Терминал", icon: "⌨️", colors: ["#6b7488", "#2c3344"], stage: 6 },
  { key: "me", title: "Мой кабинет", icon: "🙋", colors: ["#7fd1ff", "#3b8be0"], size: [900, 600] },
  { key: "employees", title: "Сотрудники", icon: "👥", colors: ["#a9b4c8", "#667189"], size: [900, 560] },
];

const MIN_CHAT = 300;

interface SearchHit {
  type: "file" | "task" | "metric" | "return" | "agent_run";
  id: string;
  title: string;
  snippet: string;
}
const HIT_APP: Record<SearchHit["type"], string> = { file: "files", task: "tasks", metric: "gen", return: "agents", agent_run: "agents" };

function renderApp(app: AppDef, user: User): ReactNode {
  if (app.key === "integrations") return <IntegrationsApp />;
  if (["gen", "tech", "ops", "fin"].includes(app.key)) return <DirectorApp key={app.key} direction={app.key} title={app.title} />;
  if (app.key === "tasks") return <TasksApp me={user} />;
  if (app.key === "remote") return <RemoteApp me={user} />;
  if (app.key === "instr1c") return <InstructionsApp me={user} />;
  if (app.key === "journal") return <ChangelogApp />;
  if (app.key === "me") return <MeApp me={user} />;
  if (app.key === "agents") return <AgentsApp me={user} />;
  if (app.key === "files") return <FilesApp />;
  if (app.key === "employees") return <EmployeesApp me={user} />;
  return <Placeholder appKey={app.key} title={app.title} stage={app.stage ?? 0} />;
}

export default function Desktop({ user, onLogout }: { user: User; onLogout: () => void }) {
  const apps = useMemo(() => APPS.filter((a) => (a.key !== "terminal" && a.key !== "remote") || user.role === "owner"), [user.role]);
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
    const n = windows.length % 6;
    const deskW = window.innerWidth - (chatOpen ? chatWidth : 0) - 60;
    const deskH = window.innerHeight - 170;
    const x = 16 + n * 26;
    const y = 16 + n * 26;
    const win: WindowState = {
      id: `${app.key}-${Date.now()}`,
      appKey: app.key,
      title: app.title,
      x,
      y,
      w: Math.max(420, Math.min(app.size?.[0] ?? 760, deskW - x)),
      h: Math.max(320, Math.min(app.size?.[1] ?? 500, deskH - y + 16)),
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

  const [hits, setHits] = useState<SearchHit[]>([]);
  useEffect(() => {
    const q = query.trim();
    if (!searchOpen || q.length < 2) {
      setHits([]);
      return;
    }
    const t = setTimeout(() => {
      api<SearchHit[]>(`/search?q=${encodeURIComponent(q)}&limit=8`).then(setHits).catch(() => setHits([]));
    }, 300);
    return () => clearTimeout(t);
  }, [query, searchOpen]);

  const found = useMemo(() => {
    const q = query.trim().toLowerCase();
    return q ? apps.filter((a) => a.title.toLowerCase().includes(q)) : apps;
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
            <FocusWidget onOpen={openByKey} />
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
                  onResize={(width, height) => patch(w.id, { w: width, h: height })}
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
                <p className="muted">Ищем по разделам и по данным компании: файлам, задачам, показателям, возвратам и итогам агентов.</p>
                <div className="search-results">
                  {found.map((a) => (
                    <button key={a.key} onClick={() => pick(a)}>
                      <span className="tile small" style={{ "--a": a.colors[0], "--b": a.colors[1] } as CSSProperties}>
                        {a.icon}
                      </span>
                      {a.title}
                    </button>
                  ))}
                  {hits.map((h) => (
                    <button key={`${h.type}-${h.id}`} className="search-hit" onClick={() => { const app = APPS.find((a) => a.key === HIT_APP[h.type]); if (app) pick(app); }}>
                      <span>
                        <strong>{h.title}</strong>
                        <span className="muted small">{h.snippet}</span>
                      </span>
                    </button>
                  ))}
                  {found.length === 0 && hits.length === 0 && <p className="muted">Ничего не найдено</p>}
                </div>
              </div>
            </div>
          )}

          <div className="dock">
            {apps.map((app) => {
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
