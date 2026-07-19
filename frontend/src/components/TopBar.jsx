import React, { useMemo } from "react";
import { Minus, Square, X } from "lucide-react";
import { GlassButton, GlassPanel, StatusBadge } from "../ui";

function Meter({ level = 0, bars = 24 }) {
  const arr = useMemo(() => Array.from({ length: bars }, (_, i) => i), [bars]);
  const normalized = Math.max(0, Math.min(1, Math.pow(level * 1.95, 0.65)));

  return (
    <div className="nova-titlebar-meter" aria-hidden="true">
      {arr.map((i) => {
        const t = i / (bars - 1);
        const bell = Math.sin(t * Math.PI);
        const h = 2 + Math.round(15 * Math.min(1, normalized * (0.32 + bell)));
        return (
          <span
            key={i}
            style={{ height: `${h}px`, opacity: 0.22 + bell * 0.78 }}
          />
        );
      })}
    </div>
  );
}

function WindowButton({ title, onClick, danger = false, children }) {
  return (
    <GlassButton
      type="button"
      variant="ghost"
      title={title}
      aria-label={title}
      onClick={onClick}
      className={[
        "titlebar-control no-drag nova-titlebar-control",
        danger && "nova-titlebar-control--danger",
      ].join(" ")}
    >
      {children}
    </GlassButton>
  );
}

export default function TopBar({
  version = "v2",
  project = "SYSTEM ONLINE",
  systemOnline = true,
  micMuted = false,
  micLevel = 0,
  micRequesting = false,
  voiceStatus = "idle",
  voicePhase = "IDLE_LISTENING",
  voiceSessionActive = false,
  ttsPlaying = false,
  wakePulse = false,
  timeText = "",
  activity = null,
  eventsConnected = false,
}) {
  const activityChip = useMemo(() => {
    if (!activity) return null;
    if (activity.vision) return { label: "Vision", tone: "warning" };
    if (activity.web) return { label: "Searching Web", tone: "warning" };
    if (activity.tool) return { label: `Tool: ${activity.tool}`, tone: "warning" };
    if (activity.thinking) return { label: "Thinking", tone: "warning" };
    if (activity.memory === "write") return { label: "Memory Write", tone: "online" };
    if (activity.memory === "read") return { label: "Memory Read", tone: "online" };
    if (activity.ttsGenerating) return { label: "Voice Synth", tone: "online" };
    return null;
  }, [activity]);
  const statusText = useMemo(() => {
    if (micRequesting) return "Requesting Mic";
    if (voiceStatus === "error") return "Voice Error";
    if (micMuted) return "Muted";
    if (voiceSessionActive) return "Engaged";
    if (wakePulse || voiceStatus === "wake") return "Wake Detected";
    if (ttsPlaying) return "Speaking";
    if (voiceStatus === "transcribing") return "Transcribing";
    if (voiceStatus === "listening" || voicePhase === "CAPTURING_COMMAND") return "Listening";
    if (voicePhase === "RESPONDING") return "Thinking";
    return "Idle";
  }, [micRequesting, micMuted, voiceSessionActive, wakePulse, voiceStatus, voicePhase, ttsPlaying]);

  const statusTone = useMemo(() => {
    if (voiceStatus === "error") return "error";
    if (micMuted) return "offline";
    if (micRequesting || voiceStatus === "transcribing" || voicePhase === "RESPONDING") return "warning";
    return "online";
  }, [micMuted, micRequesting, voicePhase, voiceStatus]);

  const statusActive =
    !micMuted &&
    (micRequesting ||
      voiceSessionActive ||
      wakePulse ||
      ttsPlaying ||
      voiceStatus === "wake" ||
      voiceStatus === "listening" ||
      voiceStatus === "speaking" ||
      voiceStatus === "transcribing" ||
      voicePhase === "CAPTURING_COMMAND" ||
      voicePhase === "RESPONDING");

  const control = (name) => {
    try {
      if (name === "minimize") return window.novaDesktop?.windowMinimize?.();
      if (name === "maximize") return window.novaDesktop?.windowToggleMaximize?.();
      if (name === "close") return window.novaDesktop?.windowClose?.();
    } catch {}
    return undefined;
  };

  return (
    <header className="nova-titlebar fixed top-0 inset-x-0 z-40 no-select titlebar-drag">
      <div className="nova-titlebar-rail" aria-hidden="true" />

      <GlassPanel as="div" variant="strong" className="nova-titlebar-shell">
        <div className="nova-titlebar-scanline" aria-hidden="true" />

        <div className="nova-titlebar-brand">
          <div className="nova-titlebar-sigil" aria-hidden="true">
            <span>N</span>
          </div>
          <div className="nova-titlebar-brand-copy">
            <span className="nova-titlebar-wordmark">NOVA</span>
            <span className="nova-titlebar-subtitle">AI Assistant</span>
          </div>
          <span className="nova-titlebar-version">{version}</span>
        </div>

        <div className="nova-titlebar-mode no-drag">
          <Meter level={micMuted ? 0 : micLevel} />
          <StatusBadge
            status={statusTone}
            pulse={statusActive}
            label={statusText}
            className="nova-titlebar-mode-badge"
          />
        </div>

        <div className="nova-titlebar-actions no-drag">
          {activityChip && (
            <StatusBadge
              status={activityChip.tone}
              pulse
              label={activityChip.label}
              className="nova-titlebar-system"
              title={eventsConnected ? "Live backend activity" : undefined}
            />
          )}
          <StatusBadge
            status={systemOnline ? "online" : "offline"}
            pulse={systemOnline}
            label={project}
            className="nova-titlebar-system"
          />
          <span className="nova-titlebar-time">{timeText}</span>
          <div className="nova-titlebar-window-controls">
            <WindowButton title="Minimize" onClick={() => control("minimize")}>
              <Minus size={14} />
            </WindowButton>
            <WindowButton title="Maximize or restore" onClick={() => control("maximize")}>
              <Square size={12} />
            </WindowButton>
            <WindowButton title="Close" onClick={() => control("close")} danger>
              <X size={14} />
            </WindowButton>
          </div>
        </div>
      </GlassPanel>
    </header>
  );
}
