import React from "react";
import { motion } from "framer-motion";
import {
  Keyboard,
  Camera,
  Images,
  Mic,
  AppWindow,
  Globe,
  SlidersHorizontal,
  MessageSquare,
  Hand,
  MonitorSmartphone,
  Eye,
} from "lucide-react";
import { FloatingContainer, GlassButton, StatusBadge } from "../ui";

function DockButton({ active, onClick, title, icon: Icon }) {
  return (
    <GlassButton
      type="button"
      variant={active ? "purple" : "ghost"}
      onClick={onClick}
      title={title}
      aria-label={title}
      aria-pressed={Boolean(active)}
      className={["nova-dock-orb", active && "nova-dock-orb--active"].filter(Boolean).join(" ")}
    >
      <Icon size={18} />
    </GlassButton>
  );
}

export default function BottomDock({
  chatOpen,
  micMuted,
  cameraOn,
  gesturesOn,
  focusSessionOn,
  activeOverlay,
  onToggleChat,
  onToggleMic,
  onToggleCameraPower,
  onToggleCameraOverlay,
  onToggleGestures,
  onToggleFocusSession,
  onOpenOverlay,
  voiceStatus = "idle",
  ttsPlaying = false,
}) {
  const listening = !micMuted && (voiceStatus === "listening" || voiceStatus === "wake");
  const speaking = ttsPlaying || voiceStatus === "speaking";
  const voiceState = speaking ? "speaking" : listening ? "listening" : micMuted ? "offline" : "online";
  const voiceLabel = speaking
    ? "Voice Speaking"
    : listening
      ? "Voice Listening"
      : micMuted
        ? "Voice Offline"
        : "Voice Online";

  return (
    <div className="nova-dock-layer">
      <FloatingContainer
        as={motion.div}
        floating={false}
        tone="mixed"
        initial={{ y: 14, opacity: 0 }}
        animate={{ y: 0, opacity: 1 }}
        transition={{ duration: 0.45, ease: [0.22, 1, 0.36, 1] }}
        className="nova-dock-capsule"
      >
        <span className="nova-dock-edge nova-dock-edge--left" aria-hidden="true" />
        <span className="nova-dock-edge nova-dock-edge--right" aria-hidden="true" />
        <div className="nova-dock-reflection" aria-hidden="true" />

        <div className="nova-dock-actions">
          <DockButton active={chatOpen} onClick={onToggleChat} title="Keyboard" icon={Keyboard} />
          <DockButton active={cameraOn} onClick={onToggleCameraPower} title="Camera" icon={Camera} />
          <DockButton active={activeOverlay === "camera"} onClick={onToggleCameraOverlay} title="Gallery" icon={Images} />

          <GlassButton
            type="button"
            variant={speaking ? "gold" : "purple"}
            onClick={onToggleMic}
            title={micMuted ? "Activate voice" : "Mute voice"}
            aria-label={micMuted ? "Activate voice" : "Mute voice"}
            aria-pressed={!micMuted}
            data-voice-state={voiceState}
            className={[
              "nova-dock-voice",
              !micMuted && "nova-dock-voice--enabled",
              listening && "nova-dock-voice--listening",
              speaking && "nova-dock-voice--speaking",
            ].filter(Boolean).join(" ")}
          >
            <span className="nova-dock-voice-ring" aria-hidden="true" />
            <Mic size={25} />
          </GlassButton>

          <DockButton active={gesturesOn} onClick={onToggleGestures} title="Apps" icon={AppWindow} />
          <DockButton active={activeOverlay === "screenvision"} onClick={() => onOpenOverlay("screenvision")} title="Screen" icon={MonitorSmartphone} />
          <DockButton
            active={focusSessionOn}
            onClick={onToggleFocusSession}
            title={focusSessionOn ? "Focus session: on (periodic screen glances) — click to stop" : "Start focus session (periodic screen glances)"}
            icon={Eye}
          />
          <DockButton active={activeOverlay === "web"} onClick={() => onOpenOverlay("web")} title="Browser" icon={Globe} />
          <DockButton active={activeOverlay === "settings"} onClick={() => onOpenOverlay("settings")} title="Settings" icon={SlidersHorizontal} />
        </div>

        <div className="nova-dock-status-row" aria-live="polite">
          <span className={chatOpen ? "is-active" : ""}><MessageSquare size={11} /> Chat</span>
          <span className={gesturesOn ? "is-active" : ""}><Hand size={11} /> Gestures</span>
          {focusSessionOn ? <span className="is-active"><Eye size={11} /> Focus session</span> : null}
          <StatusBadge
            status={speaking ? "warning" : micMuted ? "offline" : "online"}
            pulse={listening || speaking}
            label={voiceLabel}
            className="nova-dock-voice-status"
          />
        </div>
      </FloatingContainer>
    </div>
  );
}
