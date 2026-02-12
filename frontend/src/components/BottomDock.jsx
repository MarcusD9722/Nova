import React from "react";

function DockButton({ active, onClick, title, children, tone = "cyan" }) {
  const ring =
    tone === "green"
      ? "border-emerald-400/35 hover:border-emerald-300/60"
      : tone === "purple"
      ? "border-purple-400/35 hover:border-purple-300/60"
      : "border-cyan-400/35 hover:border-cyan-300/60";

  const glow =
    active
      ? tone === "green"
        ? "shadow-[0_0_24px_rgba(16,185,129,0.45)]"
        : tone === "purple"
        ? "shadow-[0_0_24px_rgba(168,85,247,0.45)]"
        : "shadow-[0_0_24px_rgba(34,211,238,0.45)]"
      : "shadow-[0_0_18px_rgba(255,255,255,0.10)]";

  return (
    <button
      onClick={onClick}
      title={title}
      className={[
        "w-12 h-12 rounded-full grid place-items-center",
        "bg-black/45 border backdrop-blur-xl",
        ring,
        glow,
        "transition-transform hover:scale-105",
      ].join(" ")}
    >
      <span className="text-white/85 text-lg">{children}</span>
    </button>
  );
}

export default function BottomDock({
  micMuted,
  cameraOn,
  gesturesOn,
  activeOverlay,
  onToggleMic,
  onToggleCamera,
  onToggleGestures,
  onOpenOverlay,
}) {
  return (
    <div className="fixed left-0 right-0 bottom-6 z-30 flex justify-center pointer-events-none">
      <div className="pointer-events-auto px-5 py-3 rounded-full border border-white/10 bg-black/35 backdrop-blur-2xl shadow-[0_10px_40px_rgba(0,0,0,0.55)]">
        <div className="flex items-center gap-4">
          <DockButton
            active={!micMuted}
            onClick={onToggleMic}
            title={micMuted ? "Unmute mic" : "Mute mic"}
            tone="green"
          >
            {micMuted ? "🔇" : "🎙️"}
          </DockButton>

          <DockButton
            active={cameraOn}
            onClick={onToggleCamera}
            title={cameraOn ? "Turn camera off" : "Turn camera on"}
          >
            {cameraOn ? "📷" : "📵"}
          </DockButton>

          <DockButton
            active={activeOverlay === "settings"}
            onClick={() => onOpenOverlay("settings")}
            title="Settings"
            tone="purple"
          >
            ⚙️
          </DockButton>

          <DockButton
            active={gesturesOn}
            onClick={onToggleGestures}
            title={gesturesOn ? "Disable gestures" : "Enable gestures"}
          >
            ✋
          </DockButton>

          <DockButton
            active={activeOverlay === "smarthome"}
            onClick={() => onOpenOverlay("smarthome")}
            title="Smart Home"
          >
            🏠
          </DockButton>

          <DockButton
            active={activeOverlay === "printer"}
            onClick={() => onOpenOverlay("printer")}
            title="3D Printer"
          >
            🖨️
          </DockButton>

          <DockButton
            active={activeOverlay === "web"}
            onClick={() => onOpenOverlay("web")}
            title="Web Search"
          >
            🌐
          </DockButton>

          <DockButton
            active={activeOverlay === "camera"}
            onClick={() => onOpenOverlay("camera")}
            title="Camera"
          >
            👁️
          </DockButton>
        </div>
      </div>
    </div>
  );
}
