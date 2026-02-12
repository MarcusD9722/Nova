import React, { useEffect, useMemo, useRef, useState, useCallback } from "react";
import { Rnd } from "react-rnd";

const DEFAULT_W = 420;
const DEFAULT_H = 520;
const HEADER_H = 40;
const GRID = 8;

function clamp(v, min, max) {
  return Math.max(min, Math.min(max, v));
}
function snap(v, step = GRID) {
  return Math.round(v / step) * step;
}

export default function DraggablePanel({
  id = "panel",
  defaultX = 200,
  defaultY = 100,
  defaultWidth = DEFAULT_W,
  defaultHeight = DEFAULT_H,
  className = "",
  children,
}) {
  const key = `nova-ui:panel:${id}`;
  const [pos, setPos] = useState({ x: defaultX, y: defaultY });
  const [size, setSize] = useState({ width: defaultWidth, height: defaultHeight });
  const [dragging, setDragging] = useState(false);
  const [z, setZ] = useState(1);

  // Restore persisted layout
  useEffect(() => {
    try {
      const raw = localStorage.getItem(key);
      if (raw) {
        const { x, y, width, height, zIndex } = JSON.parse(raw);
        if (Number.isFinite(x) && Number.isFinite(y)) setPos({ x, y });
        if (Number.isFinite(width) && Number.isFinite(height)) setSize({ width, height });
        if (Number.isFinite(zIndex)) setZ(zIndex);
      }
    } catch {}
  }, [key]);

  // Persist layout
  useEffect(() => {
    try {
      localStorage.setItem(key, JSON.stringify({ ...pos, ...size, zIndex: z }));
    } catch {}
  }, [key, pos, size, z]);

  const bringToFront = useCallback(() => {
    setZ((prev) => (prev >= 9999 ? prev : prev + 1));
  }, []);

  // Keep inside viewport on window resize
  useEffect(() => {
    const onResize = () => {
      setPos((p) => {
        const xMax = Math.max(0, window.innerWidth - size.width);
        const yMax = Math.max(0, window.innerHeight - size.height);
        return { x: clamp(p.x, 0, xMax), y: clamp(p.y, 0, yMax) };
      });
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [size]);

  const magnetize = useCallback((x, y, w, h) => {
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const edge = 12;

    const xClamped = clamp(x, 0, Math.max(0, vw - w));
    const yClamped = clamp(y, 0, Math.max(0, vh - h));

    const magnetX =
      Math.abs(xClamped) < edge ? 0 :
      Math.abs(vw - w - xClamped) < edge ? vw - w :
      xClamped;

    const magnetY =
      Math.abs(yClamped) < edge ? 0 :
      Math.abs(vh - h - yClamped) < edge ? vh - h :
      yClamped;

    return { x: magnetX, y: magnetY };
  }, []);

  const dragHandleClass = useMemo(() => `drag-handle-${id}`, [id]);

  return (
    <Rnd
      default={{
        x: defaultX,
        y: defaultY,
        width: defaultWidth,
        height: defaultHeight,
      }}
      position={{ x: pos.x, y: pos.y }}
      size={{ width: size.width, height: size.height }}
      onDragStart={() => {
        bringToFront();
        setDragging(true);
      }}
      onDrag={(e, data) => {
        const { x, y } = magnetize(data.x, data.y, size.width, size.height);
        setPos({ x, y });
      }}
      onDragStop={() => {
        setDragging(false);
        setPos((p) => ({ x: snap(p.x), y: snap(p.y) }));
      }}
      onResizeStart={bringToFront}
      onResize={(e, dir, ref, delta, position) => {
        const width = ref.offsetWidth;
        const height = ref.offsetHeight;
        const snappedW = snap(width);
        const snappedH = snap(height);
        const { x, y } = position;
        setSize({ width: snappedW, height: snappedH });
        setPos({ x, y });
      }}
      bounds="window"
      minWidth={320}
      minHeight={240}
      enableResizing={{
        top: true, right: true, bottom: true, left: true,
        topRight: true, bottomRight: true, bottomLeft: true, topLeft: true,
      }}
      dragHandleClassName={dragHandleClass}
      style={{
        position: "absolute",
        zIndex: z,
        userSelect: dragging ? "none" : "auto",
        borderRadius: "18px",
        backdropFilter: "blur(10px)",
        background: "linear-gradient(180deg, rgba(20,20,36,.6), rgba(8,8,20,.55))",
        border: "1px solid rgba(124,58,237,.35)",
        boxShadow: "0 2px 40px 8px #7C3AED55, inset 0 1px 0 rgba(255,255,255,.04)",
        overflow: "hidden",
      }}
      className={className}
      data-panel-id={id}
    >
      {/* Header bar without label */}
      <div
        className={dragHandleClass}
        style={{
          height: HEADER_H,
          display: "flex",
          alignItems: "center",
          justifyContent: "flex-start",
          padding: "0 12px",
          cursor: "grab",
          borderBottom: "1px solid rgba(124,58,237,.25)",
          background: "linear-gradient(180deg, rgba(255,255,255,.06), rgba(255,255,255,.02))",
          backdropFilter: "blur(6px)",
        }}
        onMouseDown={(e) => e.preventDefault()}
        onPointerDown={bringToFront}
      >
        <div style={{ display: "flex", gap: 8 }}>
          <button
            onClick={() => {
              const w = size.width, h = size.height;
              const x = Math.max(0, (window.innerWidth - w) / 2);
              const y = Math.max(0, (window.innerHeight - h) / 2);
              setPos({ x: snap(x), y: snap(y) });
            }}
            className="text-xs text-white/70 hover:text-white"
            aria-label="Center panel"
            title="Center"
          >
            ⊕
          </button>
          <button
            onClick={() => {
              setPos({ x: defaultX, y: defaultY });
              setSize({ width: defaultWidth, height: defaultHeight });
            }}
            className="text-xs text-white/70 hover:text-white"
            aria-label="Reset layout"
            title="Reset"
          >
            ↺
          </button>
        </div>
      </div>

      {/* Content */}
      <div style={{ width: "100%", height: `calc(100% - ${HEADER_H}px)` }}>
        {children}
      </div>
    </Rnd>
  );
}
