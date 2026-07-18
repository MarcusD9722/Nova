import React, { useMemo, useRef } from "react";
import { useFrame } from "@react-three/fiber";
import * as THREE from "three";

export default function AvatarRings({ avatarState }) {
  const rings = useRef(null);
  const innerRing = useRef(null);
  const particles = useRef(null);
  const positions = useMemo(() => {
    const data = new Float32Array(110 * 3);
    for (let index = 0; index < 110; index += 1) {
      const angle = index * 2.399963;
      const radius = 1.2 + ((index * 19) % 70) / 100;
      data[index * 3] = Math.cos(angle) * radius;
      data[index * 3 + 1] = -1.05 + ((index * 31) % 210) / 100;
      data[index * 3 + 2] = Math.sin(angle) * radius * 0.36 - 0.48;
    }
    return data;
  }, []);

  useFrame(({ clock }, delta) => {
    if (avatarState.reducedMotion) return;
    const t = clock.getElapsedTime();
    const speed = avatarState.state === "listening" ? 0.72 : avatarState.state === "thinking" ? 0.46 : 0.2;
    if (rings.current) rings.current.rotation.z += delta * speed;
    if (innerRing.current) {
      innerRing.current.rotation.z -= delta * speed * 1.28;
      const pulse = avatarState.state === "thinking" ? 1 + Math.sin(t * 4.2) * 0.045 : 1;
      innerRing.current.scale.setScalar(pulse);
    }
    if (particles.current) {
      particles.current.rotation.y += delta * speed * 0.28;
      const convergence = 1 - avatarState.config.particlePull * 0.42;
      particles.current.scale.lerp(new THREE.Vector3(convergence, convergence, convergence), delta * 2.2);
    }
  });

  return (
    <group position={[0, 0.02, -0.72]}>
      <group ref={rings} rotation={[0.08, 0, 0]}>
        <mesh>
          <torusGeometry args={[1.48, 0.008, 6, 112]} />
          <meshBasicMaterial color="#8b5cf6" transparent opacity={0.3} blending={THREE.AdditiveBlending} depthWrite={false} />
        </mesh>
        <mesh rotation={[0, 0, 0.72]}>
          <torusGeometry args={[1.62, 0.007, 6, 84, Math.PI * 1.36]} />
          <meshBasicMaterial color="#f5c542" transparent opacity={0.28} blending={THREE.AdditiveBlending} depthWrite={false} />
        </mesh>
      </group>
      <mesh ref={innerRing}>
        <torusGeometry args={[1.19, 0.006, 6, 92, Math.PI * 1.72]} />
        <meshBasicMaterial color="#52a8ff" transparent opacity={0.23} blending={THREE.AdditiveBlending} depthWrite={false} />
      </mesh>
      <points ref={particles}>
        <bufferGeometry>
          <bufferAttribute attach="attributes-position" array={positions} count={110} itemSize={3} />
        </bufferGeometry>
        <pointsMaterial color="#a78bfa" size={0.014} transparent opacity={0.4} depthWrite={false} blending={THREE.AdditiveBlending} />
      </points>
    </group>
  );
}
