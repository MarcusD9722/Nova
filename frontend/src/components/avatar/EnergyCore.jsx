import React, { useRef } from "react";
import { useFrame } from "@react-three/fiber";
import * as THREE from "three";

// Minimal fallback shown ONLY if the avatar GLB fails to load: a pulsing
// energy core, no face. (The old procedural bust was retired by request.)
export default function EnergyCore({ avatarState }) {
  const core = useRef(null);
  const ring = useRef(null);

  useFrame(({ clock }, delta) => {
    const t = clock.getElapsedTime();
    const speaking = avatarState?.state === "speaking";
    if (core.current) {
      const pulse = 1 + Math.sin(t * (speaking ? 6 : 2)) * 0.08;
      core.current.scale.setScalar(pulse);
      core.current.material.emissiveIntensity = speaking ? 2.6 : 1.6 + Math.sin(t * 1.4) * 0.3;
    }
    if (ring.current) ring.current.rotation.z += delta * 0.4;
  });

  return (
    <group position={[0, 0.2, 0]}>
      <mesh ref={core}>
        <icosahedronGeometry args={[0.28, 2]} />
        <meshStandardMaterial color="#8b5cf6" emissive="#8b5cf6" emissiveIntensity={1.8} transparent opacity={0.85} toneMapped={false} />
      </mesh>
      <mesh ref={ring}>
        <torusGeometry args={[0.52, 0.008, 8, 96]} />
        <meshBasicMaterial color="#f5c542" transparent opacity={0.6} blending={THREE.AdditiveBlending} />
      </mesh>
    </group>
  );
}
