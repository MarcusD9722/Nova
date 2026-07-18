import React from "react";

const PARTICLES = Array.from({ length: 20 }, (_, index) => ({
  left: `${8 + ((index * 37) % 84)}%`,
  top: `${10 + ((index * 53) % 72)}%`,
  delay: `${-(index % 8) * 0.72}s`,
  duration: `${6.4 + (index % 5) * 1.15}s`,
  scale: 0.65 + (index % 4) * 0.18,
}));

const STATE_LABELS = {
  idle: "Standby",
  listening: "Listening",
  thinking: "Processing",
  speaking: "Speaking",
  working: "Using Tools",
  memory: "Accessing Memory",
  vision: "Analyzing Vision",
  searching: "Searching Web",
};

export default function HologramStage({ state = "idle", avatar, waveform }) {
  return (
    <div className="nova-hologram-stage" data-stage-state={state}>
      <div className="nova-hologram-header">
        <span className="nova-hologram-header-trace" aria-hidden="true" />
        <span className="nova-hologram-wordmark">N O V A</span>
        <span className="nova-hologram-state">{STATE_LABELS[state] || STATE_LABELS.idle}</span>
      </div>

      <div className="nova-hologram-chamber">
        <div className="nova-hologram-depth" aria-hidden="true" />
        <div className="nova-hologram-halo" aria-hidden="true" />

        <div className="nova-hologram-ring nova-hologram-ring--outer" aria-hidden="true" />
        <div className="nova-hologram-ring nova-hologram-ring--segments" aria-hidden="true" />
        <div className="nova-hologram-ring nova-hologram-ring--ticks" aria-hidden="true" />
        <div className="nova-hologram-ring nova-hologram-ring--inner" aria-hidden="true" />

        <div className="nova-hologram-arc nova-hologram-arc--one" aria-hidden="true" />
        <div className="nova-hologram-arc nova-hologram-arc--two" aria-hidden="true" />
        <div className="nova-hologram-crosshair" aria-hidden="true" />

        <div className="nova-hologram-particles" aria-hidden="true">
          {PARTICLES.map((particle, index) => (
            <span
              key={index}
              style={{
                left: particle.left,
                top: particle.top,
                "--particle-delay": particle.delay,
                "--particle-duration": particle.duration,
                "--particle-scale": particle.scale,
              }}
            />
          ))}
        </div>

        <div className="nova-hologram-scanline" aria-hidden="true" />
        <div className="nova-hologram-avatar-slot">{avatar}</div>
      </div>

      <div className="nova-hologram-spectrum">
        <div className="nova-hologram-spectrum-header">
          <span>Voice spectrum</span>
          <span>{STATE_LABELS[state] || STATE_LABELS.idle}</span>
        </div>
        <div className="nova-hologram-waveform">{waveform}</div>
        <span className="nova-hologram-spectrum-accent" aria-hidden="true" />
      </div>
    </div>
  );
}
