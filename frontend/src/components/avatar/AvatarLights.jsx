import React from "react";

export default function AvatarLights({ state = "idle" }) {
  const listening = state === "listening";
  const speaking = state === "speaking";

  return (
    <>
      <ambientLight intensity={0.2} />
      <hemisphereLight args={["#a78bfa", "#090318", 0.7]} />
      <directionalLight position={[0, 2.8, -2.6]} intensity={0.76} color="#52a8ff" />
      <pointLight position={[1.9, 2.6, 2.8]} intensity={listening ? 2.05 : 1.46} color="#8b5cf6" />
      <pointLight position={[-2.2, 0.8, 2.2]} intensity={0.86} color="#52a8ff" />
      <pointLight position={[0, -1.15, 2]} intensity={speaking ? 1.65 : 0.52} color="#f5c542" />
    </>
  );
}
