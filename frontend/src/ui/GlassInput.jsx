import React, { forwardRef } from "react";

function cx(...parts) {
  return parts.filter(Boolean).join(" ");
}

const GlassInput = forwardRef(function GlassInput(
  {
    as: Tag = "input",
    className = "",
    style,
    tone = "purple",
    children,
    ...rest
  },
  ref
) {
  const props = {
    ref,
    className: cx("ui-glass-input", `ui-tone-${tone}`, className),
    style,
    ...rest,
  };

  if (Tag === "input") return <Tag {...props} />;
  return <Tag {...props}>{children}</Tag>;
});

export default GlassInput;
