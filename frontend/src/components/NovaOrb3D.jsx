import React, { useMemo, useRef } from "react";
import { Canvas, useFrame } from "@react-three/fiber";
import { EffectComposer, Bloom } from "@react-three/postprocessing";
import * as THREE from "three";

const STATE_COLORS = {
  idle: "#8b5cf6",
  listening: "#a78bfa",
  thinking: "#7c3aed",
  speaking: "#f5c542",
};

function HudRings({ color = "#8b5cf6", speed = 0.32 }) {
  const ringA = useRef(null);
  const ringB = useRef(null);
  const ringC = useRef(null);

  useFrame((_, dt) => {
    if (ringA.current) ringA.current.rotation.z += dt * speed;
    if (ringB.current) ringB.current.rotation.z -= dt * speed * 0.72;
    if (ringC.current) ringC.current.rotation.z += dt * speed * 1.2;
  });

  return (
    <group>
      <mesh ref={ringA} position={[0, 0.35, 0]}>
        <torusGeometry args={[1.42, 0.012, 10, 140]} />
        <meshBasicMaterial color={color} transparent opacity={0.5} />
      </mesh>
      <mesh ref={ringB} rotation={[Math.PI * 0.46, 0.5, 0]} position={[0, 0.3, 0]}>
        <torusGeometry args={[1.6, 0.009, 8, 110]} />
        <meshBasicMaterial color="#f5c542" transparent opacity={0.34} />
      </mesh>
      <mesh ref={ringC} rotation={[Math.PI * 0.52, -0.2, 0]} position={[0, 0.32, 0]}>
        <torusGeometry args={[1.18, 0.01, 8, 84]} />
        <meshBasicMaterial color={color} transparent opacity={0.24} />
      </mesh>
    </group>
  );
}

function ParticleHalo({ count = 700 }) {
  const pointsRef = useRef(null);
  const positions = useMemo(() => {
    const arr = new Float32Array(count * 3);
    for (let i = 0; i < count; i += 1) {
      const r = 2.3 + Math.random() * 2.2;
      const th = Math.random() * Math.PI * 2;
      const y = (Math.random() - 0.5) * 2.8;
      arr[i * 3] = Math.cos(th) * r;
      arr[i * 3 + 1] = y;
      arr[i * 3 + 2] = Math.sin(th) * r;
    }
    return arr;
  }, [count]);

  useFrame((_, dt) => {
    if (pointsRef.current) {
      pointsRef.current.rotation.y += dt * 0.07;
      pointsRef.current.rotation.x += dt * 0.01;
    }
  });

  return (
    <points ref={pointsRef}>
      <bufferGeometry>
        <bufferAttribute attach="attributes-position" array={positions} count={count} itemSize={3} />
      </bufferGeometry>
      <pointsMaterial color="#a78bfa" size={0.018} sizeAttenuation transparent opacity={0.65} depthWrite={false} />
    </points>
  );
}

function HologramAvatar({ state = "idle", micLevel = 0, ttsPlaying = false }) {
  const root = useRef(null);
  const head = useRef(null);
  const eyeL = useRef(null);
  const eyeR = useRef(null);
  const mouth = useRef(null);
  const shoulders = useRef(null);

  useFrame(({ clock }, dt) => {
    const t = clock.getElapsedTime();
    const amp = Math.max(micLevel, ttsPlaying ? 0.4 + Math.sin(t * 8) * 0.2 : 0);
    const blinkGate = Math.sin(t * 0.7) > 0.97 ? 0.15 : 1;

    if (root.current) {
      root.current.position.y = Math.sin(t * 1.8) * 0.04;
      root.current.rotation.y = Math.sin(t * 0.5) * 0.1;
    }

    if (head.current) {
      head.current.rotation.x = Math.sin(t * 0.9) * 0.05;
      head.current.rotation.z = Math.sin(t * 0.4) * 0.03;
      head.current.scale.setScalar(1 + Math.sin(t * 1.8) * 0.01);
    }

    if (eyeL.current && eyeR.current) {
      eyeL.current.scale.y = blinkGate;
      eyeR.current.scale.y = blinkGate;
      eyeL.current.position.x = -0.22 + Math.sin(t * 0.6) * 0.01;
      eyeR.current.position.x = 0.22 + Math.sin(t * 0.6) * 0.01;
      eyeL.current.material.emissiveIntensity = state === "listening" ? 4.5 : 2.9;
      eyeR.current.material.emissiveIntensity = state === "listening" ? 4.5 : 2.9;
    }

    if (mouth.current) {
      const open = Math.max(0.08, amp * 0.9);
      mouth.current.scale.y = 0.3 + open;
      mouth.current.material.opacity = 0.35 + Math.min(0.55, open * 0.7);
    }

    if (shoulders.current) {
      shoulders.current.rotation.z = Math.sin(t * 1.2) * 0.04;
      shoulders.current.rotation.y += dt * 0.08;
    }
  });

  const tint = STATE_COLORS[state] || STATE_COLORS.idle;

  return (
    <group ref={root} position={[0, -0.15, 0]}>
      <group ref={head} position={[0, 0.55, 0]}>
        <mesh>
          <sphereGeometry args={[0.52, 64, 64]} />
          <meshStandardMaterial
            color="#6d4ed4"
            emissive={tint}
            emissiveIntensity={0.58}
            roughness={0.12}
            metalness={0.35}
            transparent
            opacity={0.68}
          />
        </mesh>

        <mesh ref={eyeL} position={[-0.22, 0.02, 0.45]}>
          <sphereGeometry args={[0.055, 32, 32]} />
          <meshStandardMaterial color="#d4c6ff" emissive="#a78bfa" emissiveIntensity={3.1} />
        </mesh>
        <mesh ref={eyeR} position={[0.22, 0.02, 0.45]}>
          <sphereGeometry args={[0.055, 32, 32]} />
          <meshStandardMaterial color="#d4c6ff" emissive="#a78bfa" emissiveIntensity={3.1} />
        </mesh>

        <mesh ref={mouth} position={[0, -0.24, 0.46]}>
          <planeGeometry args={[0.21, 0.07]} />
          <meshBasicMaterial color="#f5c542" transparent opacity={0.4} />
        </mesh>
      </group>

      <mesh position={[0, -0.05, 0]}>
        <cylinderGeometry args={[0.16, 0.19, 0.42, 36]} />
        <meshStandardMaterial color="#6030df" emissive="#8b5cf6" emissiveIntensity={0.4} transparent opacity={0.6} />
      </mesh>

      <group ref={shoulders} position={[0, -0.52, 0]}>
        <mesh>
          <torusGeometry args={[0.72, 0.22, 22, 90, Math.PI]} />
          <meshStandardMaterial color="#5422c9" emissive="#8b5cf6" emissiveIntensity={0.5} transparent opacity={0.62} />
        </mesh>
      </group>
    </group>
  );
}

export default function NovaOrb3D({
  state = "idle",
  size = 520,
  micLevel = 0,
  ttsPlaying = false,
}) {
  const activeColor = STATE_COLORS[state] || STATE_COLORS.idle;
  const ringSpeed = state === "listening" ? 0.75 : state === "thinking" ? 0.5 : 0.32;

  return (
    <div style={{ width: size, height: size }} className="relative">
      <div className="absolute inset-0 pointer-events-none rounded-full bg-[radial-gradient(circle,rgba(139,92,246,0.26),rgba(10,6,24,0.0)_68%)]" />
      <Canvas
        camera={{ position: [0, 0.2, 4.5], fov: 45 }}
        dpr={[1, 2]}
        style={{ background: "transparent" }}
        gl={{ antialias: true, alpha: true, powerPreference: "high-performance" }}
      >
        <ambientLight intensity={0.75} />
        <pointLight position={[2, 3, 2]} intensity={2.1} color={activeColor} />
        <pointLight position={[-3, 1, 3]} intensity={1.2} color="#f5c542" />

        <HologramAvatar state={state} micLevel={micLevel} ttsPlaying={ttsPlaying} />
        <HudRings color={activeColor} speed={ringSpeed} />
        <ParticleHalo />

        <EffectComposer multisampling={0}>
          <Bloom intensity={0.72} luminanceThreshold={0.33} luminanceSmoothing={0.3} mipmapBlur />
        </EffectComposer>
      </Canvas>

      <div className="pointer-events-none absolute inset-x-[18%] top-1/2 h-px bg-gradient-to-r from-transparent via-nova-purple/70 to-transparent animate-pulse" />
      <div className="pointer-events-none absolute inset-x-[26%] top-[62%] h-px bg-gradient-to-r from-transparent via-nova-gold/45 to-transparent" />
    </div>
  );
}
