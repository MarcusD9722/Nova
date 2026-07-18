import React, { forwardRef } from "react";

function cx(...parts) {
  return parts.filter(Boolean).join(" ");
}

const GlassButton = forwardRef(function GlassButton(
  {
    as: Tag = "button",
    type = "button",
    className = "",
    style,
    variant = "purple",
    children,
    ...rest
  },
  ref
) {
  return (
    <Tag
      ref={ref}
      type={Tag === "button" ? type : undefined}
      className={cx("ui-glass-button", `ui-glass-button--${variant}`, className)}
      style={style}
      {...rest}
    >
      {children}
    </Tag>
  );
});

export default GlassButton;
