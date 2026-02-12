import React, { useEffect } from "react";

export default function OverlayHost({ open, title, onClose, children }) {
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape" && open) onClose?.();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  return (
    <>
      <div
        className={[
          "fixed inset-0 z-40 transition-opacity",
          open ? "opacity-100 pointer-events-auto" : "opacity-0 pointer-events-none",
        ].join(" ")}
        aria-hidden={!open}
        onMouseDown={() => onClose?.()}
        style={{ background: "rgba(0,0,0,0.55)" }}
      />
      <div
        className={[
          "fixed left-0 right-0 bottom-0 z-50",
          "transition-transform duration-200 ease-out",
          open ? "translate-y-0" : "translate-y-full",
        ].join(" ")}
        role="dialog"
        aria-modal="true"
        aria-label={title || "Overlay"}
      >
        <div
          className="mx-auto w-[min(980px,94vw)] rounded-t-3xl border border-white/10 bg-black/55 backdrop-blur-2xl shadow-[0_-10px_40px_rgba(0,0,0,0.6)]"
          onMouseDown={(e) => e.stopPropagation()}
        >
          <div className="flex items-center justify-between px-4 py-3 border-b border-white/10">
            <div className="text-xs tracking-widest uppercase text-cyan-200/80">
              {title}
            </div>
            <button
              className="text-white/70 hover:text-white text-sm px-3 py-1 rounded-lg bg-white/5 border border-white/10"
              onClick={() => onClose?.()}
            >
              Close
            </button>
          </div>
          <div className="p-4 max-h-[52vh] overflow-auto">
            {children}
          </div>
        </div>
      </div>
    </>
  );
}
