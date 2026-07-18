import React, { forwardRef } from "react";

function cx(...parts) {
  return parts.filter(Boolean).join(" ");
}

const GlowCard = forwardRef(function GlowCard(
  {
    as: Tag = "section",
    className = "",
    style,
    tone = "purple",
    interactive = false,
    children,
    ...rest
  },
  ref
) {
  return (
    <Tag
      ref={ref}
      className={cx(
        "ui-glow-card",
        `ui-tone-${tone}`,
        interactive && "ui-glow-card--interactive",
        className
      )}
      style={style}
      {...rest}
    >
      {children}
    </Tag>
  );
});

export default GlowCard;
