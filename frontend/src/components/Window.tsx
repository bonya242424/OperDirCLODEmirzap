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
  onResize: (w: number, h: number) => void;
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
  onResize,
  onClose,
  onMinimize,
  onToggleMaximize,
}: Props) {
  const drag = useRef<{ dx: number; dy: number } | null>(null);
  const size = useRef<{ x: number; y: number; w: number; h: number } | null>(null);

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
        <div className="traffic">
          <button className="light red" title="Закрыть" aria-label="Закрыть" onClick={onClose}>
            <span>×</span>
          </button>
          <button className="light yellow" title="Свернуть" aria-label="Свернуть" onClick={onMinimize}>
            <span>−</span>
          </button>
          <button
            className="light green"
            title={win.maximized ? "Вернуть размер" : "Во весь экран"}
            aria-label="Во весь экран"
            onClick={onToggleMaximize}
          >
            <span>{win.maximized ? "↙" : "↗"}</span>
          </button>
        </div>
        <span className="window-name">
          <span aria-hidden="true">{icon}</span> {win.title}
        </span>
        <span className="traffic-spacer" />
      </div>
      <div className="window-body">{children}</div>
      {!win.maximized && (
        <div
          className="window-grip"
          onPointerDown={(e) => {
            size.current = { x: e.clientX, y: e.clientY, w: win.w, h: win.h };
            e.currentTarget.setPointerCapture(e.pointerId);
          }}
          onPointerMove={(e) => {
            if (!size.current) return;
            onResize(
              Math.max(420, size.current.w + e.clientX - size.current.x),
              Math.max(300, size.current.h + e.clientY - size.current.y),
            );
          }}
          onPointerUp={() => (size.current = null)}
        />
      )}
    </div>
  );
}
