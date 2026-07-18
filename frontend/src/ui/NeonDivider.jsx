import React, { forwardRef } from "react";

function cx(...parts) {
  return parts.filter(Boolean).join(" ");
}

const NeonDivider = forwardRef(function NeonDivider(
  {
    className = "",
    style,
    vertical = false,
    tone = "purple",
    children,
    ...rest
  },
  ref
) {
  return (
    <div
      ref={ref}
      role="separator"
      aria-orientation={vertical ? "vertical" : "horizontal"}
      className={cx(
        "ui-neon-divider",
        vertical && "ui-neon-divider--vertical",
        tone === "gold" && "ui-neon-divider--gold",
        tone === "mixed" && "ui-neon-divider--mixed",
        className
      )}
      style={style}
      {...rest}
    >
      {children != null && <span className="ui-neon-divider__label">{children}</span>}
    </div>
  );
});

export default NeonDivider;
