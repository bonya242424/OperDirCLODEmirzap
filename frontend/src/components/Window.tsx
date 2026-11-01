import { PointerEvent, ReactNode, useRef } from "react";

export interface WindowState {
  id: string;
  appKey: string;
  title: string;
  x: number;
  y: number;
  w: number;
  h: number;
  z: number;
  minimized: boolean;
  maximized: boolean;
}

interface Props {
  win: WindowState;
  active: boolean;
  icon: string;
  children: ReactNode;
  onFocus: () => void;
  onMove: (x: number, y: number) => void;
  onClose: () => void;
  onMinimize: () => void;
  onToggleMaximize: () => void;
}

export default function Window({
  win,
  active,
  icon,
  children,
  onFocus,
  onMove,
  onClose,
  onMinimize,
  onToggleMaximize,
}: Props) {
  const drag = useRef<{ dx: number; dy: number } | null>(null);

  function down(e: PointerEvent<HTMLDivElement>) {
    if ((e.target as HTMLElement).closest("button") || win.maximized) return;
    drag.current = { dx: e.clientX - win.x, dy: e.clientY - win.y };
    e.currentTarget.setPointerCapture(e.pointerId);
  }

  function move(e: PointerEvent<HTMLDivElement>) {
    if (!drag.current) return;
    onMove(Math.max(0, e.clientX - drag.current.dx), Math.max(0, e.clientY - drag.current.dy));
  }

  const style = win.maximized
    ? { zIndex: win.z }
    : { left: win.x, top: win.y, width: win.w, height: win.h, zIndex: win.z };

  return (
    <div
      className={`window${active ? " active" : ""}${win.maximized ? " maximized" : ""}`}
      style={{ ...style, display: win.minimized ? "none" : undefined }}
      onPointerDownCapture={onFocus}
    >
      <div
        className="window-title"
        onPointerDown={down}
        onPointerMove={move}
        onPointerUp={() => (drag.current = null)}
        onDoubleClick={onToggleMaximize}
      >
        <span className="window-name">
          <span aria-hidden="true">{icon}</span> {win.title}
        </span>
        <div className="window-buttons">
          <button onClick={onToggleMaximize}>
            <i className="dot green" /> Во весь экран
          </button>
          <button onClick={onMinimize}>
            <i className="dot yellow" /> Свернуть
          </button>
          <button onClick={onClose}>
            <i className="dot red" /> Закрыть
          </button>
        </div>
      </div>
      <div className="window-body">{children}</div>
    </div>
  );
}
