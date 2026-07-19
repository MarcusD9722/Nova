import React, { forwardRef } from "react";

function cx(...parts) {
  return parts.filter(Boolean).join(" ");
}

const FloatingContainer = forwardRef(function FloatingContainer(
  {
    as: Tag = "div",
    className = "",
    style,
    tone = "purple",
    floating = true,
    hoverable = false,
    children,
    ...rest
  },
  ref
) {
  return (
    <Tag
      ref={ref}
      className={cx(
        "ui-floating-container",
        `ui-tone-${tone}`,
        floating && "ui-floating-container--float",
        hoverable && "ui-floating-container--hoverable",
        className
      )}
      style={style}
      {...rest}
    >
      {children}
    </Tag>
  );
});

export default FloatingContainer;
