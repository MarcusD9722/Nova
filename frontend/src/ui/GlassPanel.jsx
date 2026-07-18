import React, { forwardRef } from "react";

function cx(...parts) {
  return parts.filter(Boolean).join(" ");
}

const GlassPanel = forwardRef(function GlassPanel(
  {
    as: Tag = "div",
    className = "",
    style,
    variant = "default",
    tone = "neutral",
    heavy = false,
    neon = false,
    glow = "",
    interactive = false,
    children,
    ...rest
  },
  ref
) {
  const surface = heavy ? "strong" : variant;

  return (
    <Tag
      ref={ref}
      className={cx(
        "ui-glass-panel",
        surface === "strong" ? "nova-glass-surface nova-glass-surface--strong" : "nova-glass-surface",
        surface === "subtle" && "nova-glass-surface--subtle",
        `ui-tone-${tone}`,
        neon && "nova-neon-border",
        glow === "purple" && "nova-glow-purple",
        glow === "gold" && "nova-glow-gold",
        glow === "mixed" && "nova-glow-mixed",
        interactive && "ui-glass-panel--interactive",
        className
      )}
      style={style}
      {...rest}
    >
      {children}
    </Tag>
  );
});

export default GlassPanel;
