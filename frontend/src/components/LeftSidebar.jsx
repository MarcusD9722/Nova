import React from "react";
import { motion } from "framer-motion";
import {
  House,
  MessageSquare,
  Brain,
  ListTodo,
  Cpu,
  Settings,
  Sparkles,
  Activity,
  MemoryStick,
  Flame,
  Wand2,
} from "lucide-react";
import { GlassButton, GlassPanel, GlowCard, NeonDivider, StatusBadge } from "../ui";

const NAV_ITEMS = [
  { key: "home", label: "Home", icon: House },
  { key: "chat", label: "Chat", icon: MessageSquare },
  { key: "memory", label: "Memory", icon: Brain },
  { key: "tasks", label: "Tasks", icon: ListTodo },
  { key: "improvements", label: "Improve", icon: Wand2 },
  { key: "system", label: "System", icon: Cpu },
  { key: "settings", label: "Settings", icon: Settings },
];

function CoreMetric({ icon: Icon, label, value, tone = "purple" }) {
  return (
    <GlowCard as="div" tone={tone} className="nova-sidebar-metric">
      <div className="nova-sidebar-metric-label">
        <Icon size={12} />
        {label}
      </div>
      <div className="nova-sidebar-metric-value" title={String(value)}>{value}</div>
    </GlowCard>
  );
}

export default function LeftSidebar({
  activeSection = "home",
  onSelect,
  coreStatus,
}) {
  const coreOnline = coreStatus?.status === "online";

  return (
    <aside className="nova-sidebar">
      <GlassPanel
        as={motion.section}
        variant="strong"
        glow="purple"
        initial={{ opacity: 0, x: -24 }}
        animate={{ opacity: 1, x: 0 }}
        transition={{ duration: 0.45, ease: "easeOut" }}
        className="nova-sidebar-panel nova-sidebar-navigation"
      >
        <span className="nova-sidebar-edge-accent" aria-hidden="true" />
        <div className="nova-sidebar-header">
          <div className="nova-sidebar-emblem">
            <Sparkles size={16} />
          </div>
          <div className="nova-sidebar-heading">
            <div className="nova-sidebar-eyebrow">Nova AI Assistant</div>
            <div className="nova-sidebar-title">Command Center</div>
          </div>
        </div>

        <NeonDivider tone="mixed" className="nova-sidebar-divider" />

        <nav className="nova-sidebar-nav" aria-label="Primary navigation">
          {NAV_ITEMS.map((item, idx) => {
            const Icon = item.icon;
            const active = activeSection === item.key;
            return (
              <GlassButton
                as={motion.button}
                key={item.key}
                type="button"
                variant="ghost"
                onClick={() => onSelect?.(item.key)}
                aria-current={active ? "page" : undefined}
                className={["nova-sidebar-nav-item", active && "nova-sidebar-nav-item--active"].filter(Boolean).join(" ")}
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: idx * 0.04 }}
              >
                <span className="nova-sidebar-nav-icon"><Icon size={16} /></span>
                <span>{item.label}</span>
                <span className="nova-sidebar-nav-trace" aria-hidden="true" />
              </GlassButton>
            );
          })}
        </nav>
      </GlassPanel>

      <GlassPanel
        as={motion.section}
        variant="strong"
        glow="mixed"
        initial={{ opacity: 0, y: 16 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.45, delay: 0.12 }}
        className="nova-sidebar-panel nova-sidebar-core"
      >
        <span className="nova-sidebar-edge-accent nova-sidebar-edge-accent--gold" aria-hidden="true" />
        <div className="nova-sidebar-core-header">
          <div className="nova-sidebar-heading">
            <div className="nova-sidebar-eyebrow">Nova Core</div>
            <div className="nova-sidebar-title">System Telemetry</div>
          </div>
          <StatusBadge
            status={coreOnline ? "online" : "offline"}
            pulse={coreOnline}
            label={coreStatus?.statusText || "Offline"}
            className="nova-sidebar-core-status"
          />
        </div>

        <NeonDivider tone="gold" className="nova-sidebar-divider" />

        <div className="nova-sidebar-metrics">
          <CoreMetric icon={Cpu} label="Model" value={coreStatus?.model || "Not loaded"} />
          <CoreMetric icon={MemoryStick} label="Context" value={coreStatus?.contextLength || "Unknown"} />
          <CoreMetric icon={Activity} label="GPU" value={coreStatus?.gpu || "Unknown"} />
          {coreStatus?.vram ? <CoreMetric icon={MemoryStick} label="VRAM" value={coreStatus.vram} /> : null}
          <CoreMetric icon={Flame} label="Temp" value={coreStatus?.temperature || "--"} tone="gold" />
          <CoreMetric icon={Brain} label="Tokens" value={coreStatus?.tokenUsage || "0"} tone="gold" />
        </div>
      </GlassPanel>
    </aside>
  );
}
