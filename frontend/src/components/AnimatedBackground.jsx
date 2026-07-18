import React from "react";

export default function AnimatedBackground() {
  return (
    <div className="nova-ambient-background" aria-hidden="true">
      <div className="nova-ambient-base" />

      <div className="nova-ambient-nebula nova-ambient-nebula--purple" />
      <div className="nova-ambient-nebula nova-ambient-nebula--gold" />

      <div className="nova-ambient-stars nova-ambient-stars--far" />
      <div className="nova-ambient-stars nova-ambient-stars--near" />

      <div className="nova-ambient-beam nova-ambient-beam--left" />
      <div className="nova-ambient-beam nova-ambient-beam--right" />

      <div className="nova-ambient-avatar-halo" />
      <div className="nova-ambient-horizon" />
      <div className="nova-ambient-grid" />

      <div className="nova-ambient-haze" />
      <div className="nova-ambient-vignette" />
      <div className="nova-ambient-grain" />
    </div>
  );
}
