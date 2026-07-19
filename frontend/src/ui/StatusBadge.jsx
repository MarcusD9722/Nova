import React, { forwardRef } from "react";

function cx(...parts) {
  return parts.filter(Boolean).join(" ");
}

const StatusBadge = forwardRef(function StatusBadge(
  {
    as: Tag = "span",
    status = "offline",
    label,
    className = "",
    style,
    pulse = false,
    showDot = true,
    children,
    ...rest
  },
  ref
) {
  const content = children ?? label ?? status;

  return (
    <Tag
      ref={ref}
      className={cx(
        "ui-status-badge",
        `ui-status-${status}`,
        pulse && "ui-status-badge--pulse",
        className
      )}
      style={style}
      {...rest}
    >
      {showDot && <span className="ui-status-dot" aria-hidden="true" />}
      <span>{content}</span>
    </Tag>
  );
});

export default StatusBadge;
