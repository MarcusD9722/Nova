import React, { useMemo, useRef } from "react";
import { useFrame } from "@react-three/fiber";
import * as THREE from "three";
import { createJawSyncState, jawTargetFor, smoothJaw } from "../../voice/lipSync";

const PURPLE = "#8b5cf6";
const VIOLET = "#a78bfa";
const SKIN = "#8f7ce0";
const BLUE = "#52a8ff";
const GOLD = "#f5c542";
const INK = "#170b3b";

function HologramMaterial({ color = PURPLE, opacity = 0.58, wireframe = false, emissiveIntensity = 0.85 }) {
  return (
    <meshPhysicalMaterial
      color={color}
      emissive={color}
      emissiveIntensity={emissiveIntensity}
      roughness={0.34}
      metalness={0.12}
      transmission={0.08}
      transparent
      opacity={opacity}
      wireframe={wireframe}
      depthWrite={false}
      side={THREE.DoubleSide}
      blending={THREE.NormalBlending}
    />
  );
}

function AvatarParticles({ avatarState }) {
  const points = useRef(null);
  const material = useRef(null);
  const positions = useMemo(() => {
    const data = new Float32Array(240 * 3);
    for (let index = 0; index < 240; index += 1) {
      const angle = index * 2.399963;
      const radius = 0.72 + ((index * 17) % 100) / 105;
      data[index * 3] = Math.cos(angle) * radius;
      data[index * 3 + 1] = -0.65 + ((index * 47) % 230) / 100;
      data[index * 3 + 2] = Math.sin(angle) * radius * 0.52 - 0.12;
    }
    return data;
  }, []);

  useFrame(({ clock }, delta) => {
    if (!points.current) return;
    const speed = avatarState.reducedMotion ? 0 : avatarState.state === "thinking" ? 0.42 : 0.14;
    points.current.rotation.y += delta * speed;
    const targetScale = 1 - avatarState.config.particlePull;
    const nextScale = THREE.MathUtils.lerp(points.current.scale.x, targetScale, delta * 2.4);
    points.current.scale.setScalar(nextScale);
    points.current.position.y = avatarState.reducedMotion ? 0 : Math.sin(clock.getElapsedTime() * 0.8) * 0.025;
    if (material.current) material.current.opacity = 0.34 + avatarState.config.glow * 0.16;
  });

  return (
    <points ref={points} position={[0, 0, -0.2]}>
      <bufferGeometry>
        <bufferAttribute attach="attributes-position" array={positions} count={240} itemSize={3} />
      </bufferGeometry>
      <pointsMaterial ref={material} color={VIOLET} size={0.018} sizeAttenuation transparent opacity={0.48} depthWrite={false} blending={THREE.AdditiveBlending} />
    </points>
  );
}

export default function HolographicBust({ avatarState }) {
  const root = useRef(null);
  const head = useRef(null);
  const chest = useRef(null);
  const jawGroup = useRef(null);
  const mouthInterior = useRef(null);
  const lowerLip = useRef(null);
  const upperLip = useRef(null);
  const irisLeft = useRef(null);
  const irisRight = useRef(null);
  const lidLeft = useRef(null);
  const lidRight = useRef(null);
  const browLeft = useRef(null);
  const browRight = useRef(null);
  const forehead = useRef(null);
  const shoulderGlow = useRef(null);
  const jawSync = useRef(createJawSyncState(0.02));

  useFrame(({ clock }, delta) => {
    const t = clock.getElapsedTime();
    const motion = avatarState.reducedMotion ? 0 : avatarState.config.motion;
    const speaking = avatarState.state === "speaking";
    const thinking = avatarState.state === "thinking";

    // ── Blink (natural double-blink occasionally) ─────────────────────────
    const blinkPhase = t % 4.65;
    const blink =
      avatarState.facial.blink ??
      (blinkPhase > 4.48 ? Math.max(0.06, Math.abs(Math.cos(((blinkPhase - 4.48) / 0.17) * Math.PI))) : 1);
    const lidCover = THREE.MathUtils.clamp(1.08 - blink, 0.1, 1);

    // ── Lip sync: real speech waveform first, calm settle-to-closed otherwise ──
    const target = jawTargetFor({
      externalJaw: avatarState.facial.jawOpen,
      speaking,
      audioEnergy: avatarState.audioEnergy,
      time: t,
    });
    const jaw = smoothJaw(jawSync.current, target);

    // ── Body/head idle motion ─────────────────────────────────────────────
    if (root.current) {
      root.current.position.y = -0.12 + Math.sin(t * 1.05) * 0.045 * motion;
      root.current.rotation.y = Math.sin(t * 0.42) * 0.075 * motion;
    }
    if (head.current) {
      head.current.rotation.x = Math.sin(t * 0.56) * 0.03 * motion + (thinking ? 0.03 : 0);
      head.current.rotation.z = Math.sin(t * 0.38) * 0.02 * motion;
      head.current.rotation.y = Math.sin(t * 0.27) * 0.05 * motion;
    }
    if (chest.current) {
      const breath = 1 + Math.sin(t * 1.35) * 0.012 * motion;
      chest.current.scale.y = THREE.MathUtils.lerp(chest.current.scale.y, breath, delta * 4);
    }

    // ── Face articulation ─────────────────────────────────────────────────
    if (jawGroup.current) {
      jawGroup.current.rotation.x = jaw * 0.34;
    }
    if (mouthInterior.current) {
      mouthInterior.current.material.opacity = 0.08 + jaw * 0.6;
      mouthInterior.current.scale.y = 0.028 + jaw * 0.085;
    }
    if (lowerLip.current) {
      lowerLip.current.material.emissiveIntensity = speaking ? 1.7 : 0.8;
      lowerLip.current.scale.x = 0.105 - jaw * 0.012;
    }
    if (upperLip.current) {
      upperLip.current.material.emissiveIntensity = speaking ? 1.5 : 0.75;
    }

    // Eyes: subtle wandering gaze, snappier while listening.
    const gazeX = Math.sin(t * 0.31) * 0.014 + (avatarState.state === "listening" ? Math.sin(t * 1.7) * 0.006 : 0);
    const gazeY = Math.sin(t * 0.47) * 0.008;
    [irisLeft.current, irisRight.current].forEach((iris) => {
      if (!iris) return;
      // iris refs are groups (iris + pupil); the glow lives on child materials.
      iris.position.x = THREE.MathUtils.lerp(iris.position.x, gazeX, 0.12);
      iris.position.y = THREE.MathUtils.lerp(iris.position.y, gazeY, 0.12);
      iris.children.forEach((child) => {
        if (child.material && "emissiveIntensity" in child.material) {
          child.material.emissiveIntensity = 2.3 * avatarState.config.eyeGlow;
        }
      });
    });
    [lidLeft.current, lidRight.current].forEach((lid) => {
      if (!lid) return;
      lid.scale.y = THREE.MathUtils.lerp(lid.scale.y, lidCover, 0.55);
    });

    // Brows: raise a touch while thinking, settle while idle.
    const browLift = thinking ? 0.012 : 0;
    if (browLeft.current) {
      browLeft.current.position.y = THREE.MathUtils.lerp(browLeft.current.position.y, 0.145 + browLift, 0.1);
      browLeft.current.rotation.z = -0.1 - (thinking ? 0.06 : 0);
    }
    if (browRight.current) {
      browRight.current.position.y = THREE.MathUtils.lerp(browRight.current.position.y, 0.145 + browLift, 0.1);
      browRight.current.rotation.z = 0.1 + (thinking ? 0.06 : 0);
    }

    if (forehead.current) forehead.current.material.emissiveIntensity = thinking ? 3.8 + Math.sin(t * 4.2) : 0.72;
    if (shoulderGlow.current) shoulderGlow.current.material.emissiveIntensity = (speaking ? 2.4 : 1.1) + Math.sin(t * (speaking ? 5.5 : 1.8)) * 0.38;
  });

  return (
    <group ref={root} position={[0, -0.12, 0]}>
      <AvatarParticles avatarState={avatarState} />

      <group ref={head} position={[0, 0.66, 0]}>
        {/* Skull + face volumes (soft, organic overlap) */}
        <mesh scale={[0.5, 0.63, 0.5]}>
          <sphereGeometry args={[1, 64, 64]} />
          <HologramMaterial color={PURPLE} opacity={0.44} emissiveIntensity={0.66 * avatarState.config.glow} />
        </mesh>
        <mesh position={[0, -0.06, 0.05]} scale={[0.44, 0.56, 0.44]}>
          <sphereGeometry args={[1, 56, 56]} />
          <HologramMaterial color={SKIN} opacity={0.34} emissiveIntensity={0.62 * avatarState.config.glow} />
        </mesh>
        {/* Cheek + chin volumes to break the egg silhouette */}
        <mesh position={[-0.2, -0.16, 0.28]} scale={[0.16, 0.14, 0.12]}>
          <sphereGeometry args={[1, 32, 32]} />
          <HologramMaterial color={SKIN} opacity={0.22} emissiveIntensity={0.55} />
        </mesh>
        <mesh position={[0.2, -0.16, 0.28]} scale={[0.16, 0.14, 0.12]}>
          <sphereGeometry args={[1, 32, 32]} />
          <HologramMaterial color={SKIN} opacity={0.22} emissiveIntensity={0.55} />
        </mesh>
        {/* Fine wireframe shell — hologram signature, kept subtle */}
        <mesh scale={[0.515, 0.645, 0.515]}>
          <sphereGeometry args={[1, 30, 24]} />
          <HologramMaterial color={BLUE} opacity={0.05} wireframe emissiveIntensity={0.65} />
        </mesh>

        {/* Hair: swept-back cap + soft side curtains */}
        <mesh position={[0, 0.12, -0.1]} scale={[0.56, 0.66, 0.52]}>
          <sphereGeometry args={[1, 44, 38, 0, Math.PI * 2, 0, Math.PI * 0.52]} />
          <HologramMaterial color={INK} opacity={0.5} emissiveIntensity={0.34} />
        </mesh>
        <mesh position={[-0.42, -0.1, -0.06]} rotation={[0, 0.28, -0.1]} scale={[0.13, 0.5, 0.22]}>
          <sphereGeometry args={[1, 32, 32]} />
          <HologramMaterial color={INK} opacity={0.34} emissiveIntensity={0.26} />
        </mesh>
        <mesh position={[0.42, -0.1, -0.06]} rotation={[0, -0.28, 0.1]} scale={[0.13, 0.5, 0.22]}>
          <sphereGeometry args={[1, 32, 32]} />
          <HologramMaterial color={INK} opacity={0.34} emissiveIntensity={0.26} />
        </mesh>

        {/* Brows */}
        <mesh ref={browLeft} position={[-0.19, 0.145, 0.44]} rotation={[0, 0, -0.1]} scale={[0.13, 0.014, 0.02]}>
          <sphereGeometry args={[1, 18, 12]} />
          <meshBasicMaterial color={VIOLET} transparent opacity={0.7} />
        </mesh>
        <mesh ref={browRight} position={[0.19, 0.145, 0.44]} rotation={[0, 0, 0.1]} scale={[0.13, 0.014, 0.02]}>
          <sphereGeometry args={[1, 18, 12]} />
          <meshBasicMaterial color={VIOLET} transparent opacity={0.7} />
        </mesh>

        {/* Eyes: sclera + iris + pupil + eyelid */}
        {[-1, 1].map((side) => (
          <group key={side} position={[side * 0.185, 0.045, 0.41]}>
            <mesh scale={[0.082, 0.05, 0.04]}>
              <sphereGeometry args={[1, 28, 20]} />
              <meshStandardMaterial color="#ded4ff" emissive="#b9a8ff" emissiveIntensity={0.5} transparent opacity={0.85} />
            </mesh>
            <group ref={side < 0 ? irisLeft : irisRight} position={[0, 0, 0.028]}>
              <mesh scale={[0.036, 0.036, 0.014]}>
                <sphereGeometry args={[1, 24, 18]} />
                <meshStandardMaterial color={VIOLET} emissive={VIOLET} emissiveIntensity={2.3} toneMapped={false} />
              </mesh>
              <mesh position={[0, 0, 0.012]} scale={[0.015, 0.015, 0.008]}>
                <sphereGeometry args={[1, 16, 12]} />
                <meshBasicMaterial color={INK} />
              </mesh>
            </group>
            {/* Eyelid: slides down over the eye on blink */}
            <mesh ref={side < 0 ? lidLeft : lidRight} position={[0, 0.028, 0.012]} scale={[0.088, 0.1, 0.045]}>
              <sphereGeometry args={[1, 24, 16, 0, Math.PI * 2, 0, Math.PI * 0.5]} />
              <HologramMaterial color={SKIN} opacity={0.55} emissiveIntensity={0.5} />
            </mesh>
          </group>
        ))}

        {/* Nose: soft, small */}
        <mesh position={[0, -0.07, 0.46]} scale={[0.045, 0.085, 0.05]}>
          <sphereGeometry args={[1, 24, 20]} />
          <HologramMaterial color={SKIN} opacity={0.4} emissiveIntensity={0.6} />
        </mesh>

        {/* Mouth interior (revealed as the jaw opens) */}
        <mesh ref={mouthInterior} position={[0, -0.275, 0.415]} scale={[0.095, 0.03, 0.02]}>
          <sphereGeometry args={[1, 24, 16]} />
          <meshBasicMaterial color={INK} transparent opacity={0.1} />
        </mesh>

        {/* Upper lip (static) */}
        <mesh ref={upperLip} position={[0, -0.252, 0.442]} scale={[0.11, 0.018, 0.024]}>
          <sphereGeometry args={[1, 28, 16]} />
          <meshStandardMaterial color={GOLD} emissive={GOLD} emissiveIntensity={0.9} transparent opacity={0.7} toneMapped={false} />
        </mesh>

        {/* Jaw: chin + lower lip rotate open from a pivot near the ears */}
        <group ref={jawGroup} position={[0, -0.14, 0.02]}>
          <mesh position={[0, -0.18, 0.2]} scale={[0.24, 0.19, 0.2]}>
            <sphereGeometry args={[1, 36, 30]} />
            <HologramMaterial color={SKIN} opacity={0.3} emissiveIntensity={0.55} />
          </mesh>
          <mesh ref={lowerLip} position={[0, -0.155, 0.415]} scale={[0.105, 0.02, 0.026]}>
            <sphereGeometry args={[1, 28, 16]} />
            <meshStandardMaterial color={GOLD} emissive={GOLD} emissiveIntensity={0.9} transparent opacity={0.72} toneMapped={false} />
          </mesh>
        </group>

        {/* Forehead sigil */}
        <mesh ref={forehead} position={[0, 0.29, 0.44]} rotation={[0, 0, Math.PI / 4]}>
          <octahedronGeometry args={[0.032, 0]} />
          <meshStandardMaterial color={GOLD} emissive={GOLD} emissiveIntensity={0.7} transparent opacity={0.82} toneMapped={false} />
        </mesh>
      </group>

      {/* Neck */}
      <mesh position={[0, -0.02, 0]}>
        <cylinderGeometry args={[0.13, 0.17, 0.47, 32]} />
        <HologramMaterial color={SKIN} opacity={0.3} emissiveIntensity={0.72} />
      </mesh>

      <group ref={chest} position={[0, -0.43, -0.04]}>
        <mesh scale={[0.86, 0.22, 0.35]}>
          <sphereGeometry args={[1, 52, 34]} />
          <HologramMaterial color={PURPLE} opacity={0.4} emissiveIntensity={0.72 * avatarState.config.glow} />
        </mesh>
        <mesh position={[0, -0.32, -0.04]}>
          <cylinderGeometry args={[0.51, 0.7, 0.68, 48, 1, true]} />
          <HologramMaterial color={PURPLE} opacity={0.23} emissiveIntensity={0.7 * avatarState.config.glow} />
        </mesh>
        <mesh ref={shoulderGlow} scale={[0.88, 0.235, 0.37]}>
          <sphereGeometry args={[1, 28, 18]} />
          <HologramMaterial color={avatarState.state === "speaking" ? GOLD : BLUE} opacity={0.065} wireframe emissiveIntensity={0.82} />
        </mesh>
        <mesh position={[-0.25, -0.12, 0.25]} scale={[0.31, 0.16, 0.22]}>
          <sphereGeometry args={[1, 30, 22]} />
          <HologramMaterial color={VIOLET} opacity={0.14} emissiveIntensity={0.45} />
        </mesh>
        <mesh position={[0.25, -0.12, 0.25]} scale={[0.31, 0.16, 0.22]}>
          <sphereGeometry args={[1, 30, 22]} />
          <HologramMaterial color={VIOLET} opacity={0.14} emissiveIntensity={0.45} />
        </mesh>
        <mesh position={[0, 0.08, 0.31]} rotation={[0, 0, Math.PI]}>
          <torusGeometry args={[0.34, 0.014, 8, 72, Math.PI]} />
          <meshBasicMaterial color={GOLD} transparent opacity={0.62} blending={THREE.AdditiveBlending} />
        </mesh>
      </group>

      <mesh position={[0, -0.83, 0]} rotation={[Math.PI / 2, 0, 0]}>
        <ringGeometry args={[0.46, 0.49, 96]} />
        <meshBasicMaterial color={GOLD} transparent opacity={0.58} side={THREE.DoubleSide} blending={THREE.AdditiveBlending} />
      </mesh>
    </group>
  );
}
