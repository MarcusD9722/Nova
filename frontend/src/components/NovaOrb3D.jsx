import React, { useMemo, useRef, useEffect } from "react";
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { Text, MeshDistortMaterial, useTexture } from "@react-three/drei";
import { EffectComposer, Bloom } from "@react-three/postprocessing";
import * as THREE from "three";

/* ---------------------- tiny helpers ---------------------- */
function useReducedMotion() {
  if (typeof window === "undefined") return false;
  return window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches ?? false;
}
function usePageVisible() {
  const visible = typeof document !== "undefined" ? document.visibilityState !== "hidden" : true;
  const ref = React.useRef(visible);
  React.useEffect(() => {
    const onVis = () => (ref.current = document.visibilityState !== "hidden");
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, []);
  return ref;
}

/* ---------------------- themes by state ------------------- */
/* Swapped to cooler, techy hues (cyan/teal/magenta) */
const STATE_THEMES = {
  idle:     { core: "#5EEAD4", glow: "#60A5FA", accent: "#22D3EE", text: "#E0F2FE", pulse: 2.2, distort: 0.12, bloom: 0.35 },
  // Faint gold/amber glow to indicate active listening.
  listening:{ core: "#FCD34D", glow: "#F59E0B", accent: "#FBBF24", text: "#FFF7ED", pulse: 2.6, distort: 0.14, bloom: 0.48 },
  thinking: { core: "#67E8F9", glow: "#A78BFA", accent: "#22D3EE", text: "#BAE6FD", pulse: 3.2, distort: 0.18, bloom: 0.45 },
  speaking: { core: "#F472B6", glow: "#38BDF8", accent: "#06B6D4", text: "#FFE4FA", pulse: 4.0, distort: 0.22, bloom: 0.55 },
};

/* ---------------------- Fresnel rim (thin, sci-fi) -------- */
function FresnelMaterial({
  color = "#60A5FA",
  intensity = 0.45, // thinner glow
  power = 2.2,
  bias = 0.06,
}) {
  const mat = React.useMemo(
    () =>
      new THREE.ShaderMaterial({
        uniforms: {
          uColor: { value: new THREE.Color(color) },
          uBias: { value: bias },
          uPower: { value: power },
          uIntensity: { value: intensity },
        },
        vertexShader: `
          varying vec3 vNormal;
          varying vec3 vWorldPos;
          void main() {
            vNormal = normalize(normalMatrix * normal);
            vec4 worldPos = modelMatrix * vec4(position, 1.0);
            vWorldPos = worldPos.xyz;
            gl_Position = projectionMatrix * viewMatrix * worldPos;
          }
        `,
        fragmentShader: `
          uniform vec3 uColor;
          uniform float uBias;
          uniform float uPower;
          uniform float uIntensity;
          varying vec3 vNormal;
          varying vec3 vWorldPos;
          void main() {
            vec3 viewDir = normalize(cameraPosition - vWorldPos);
            float f = pow(uBias + (1.0 - max(dot(viewDir, vNormal), 0.0)), uPower);
            vec3 col = uColor * f * uIntensity;
            gl_FragColor = vec4(col, f);
          }
        `,
        transparent: true,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
        depthTest: false,   // always on top; avoids z-fight
      }),
    [color, intensity, power, bias]
  );
  return <primitive object={mat} attach="material" />;
}

/* ---------------------- Orbiting text ring ----------------- */
/* Reads N O V A left-to-right and faces camera */
function OrbitingNovaRing({ text = "NOVA", radius = 1.7, speed = 0.55, color = "#E0F2FE" }) {
  const refs = React.useMemo(() => [...text].map(() => React.createRef()), [text]);
  useFrame(({ clock, camera }) => {
    const t = clock.getElapsedTime();
    const base = t * speed;
    const step = 0.32;
    const offset = ((text.length - 1) * step) / 2;
    [...text].forEach((_, i) => {
      const ang = base + i * step - offset;
      const x = radius * Math.cos(ang);
      const z = -radius * Math.sin(ang);
      const r = refs[i].current;
      if (r) {
        r.position.set(x, 0.13, z);
        r.quaternion.copy(camera.quaternion);
      }
    });
  });
  return (
    <>
      {[...text].map((ch, i) => (
        <Text
          key={i}
          ref={refs[i]}
          fontSize={0.41}
          color={color}
          anchorX="center"
          anchorY="middle"
          outlineWidth={0.035}
          outlineColor="#FFFFFF"
          outlineOpacity={0.85}
          renderOrder={10}
        >
          {ch}
        </Text>
      ))}
    </>
  );
}

/* ---------------------- Hex shell (optional texture) ------- */
function HexShell({ url, radius = 1.10 }) {
  const tex = useTexture(url);
  tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
  tex.repeat.set(3, 3);
  return (
    <mesh renderOrder={2}>
      <sphereGeometry args={[radius, 64, 64]} />
      <meshStandardMaterial
        map={tex}
        transparent
        opacity={0.35}
        polygonOffset
        polygonOffsetFactor={2}
        polygonOffsetUnits={2}
        depthWrite={false}
        depthTest={true}
      />
    </mesh>
  );
}

/* ---------------------- Tech wireframe grid ---------------- */
/* Clean edges (lat/long vibe), rotates slowly */
function GridShell({ radius = 1.12, color = "#38BDF8", opacity = 0.25, speed = 0.1, segments = 40 }) {
  const ref = useRef();
  const geom = useMemo(() => new THREE.SphereGeometry(radius, segments, segments), [radius, segments]);
  const edges = useMemo(() => new THREE.EdgesGeometry(geom, 30), [geom]); // thresholdAngle=30
  const mat = useMemo(
    () =>
      new THREE.LineBasicMaterial({
        color,
        transparent: true,
        opacity,
        depthWrite: false,
      }),
    [color, opacity]
  );
  useFrame((_, dt) => {
    if (!ref.current) return;
    ref.current.rotation.y += speed * dt;
  });
  return (
    <lineSegments ref={ref} renderOrder={2}>
      <primitive attach="geometry" object={edges} />
      <primitive attach="material" object={mat} />
    </lineSegments>
  );
}

/* ---------------------- Equatorial dashed ring ------------- */
function EquatorDash({ radius = 1.28, color = "#22D3EE", opacity = 0.85, speed = 0.7 }) {
  const ref = useRef();
  const geom = useMemo(() => new THREE.CircleGeometry(radius, 256), [radius]);
  const mat = useMemo(
    () =>
      new THREE.LineDashedMaterial({
        color,
        transparent: true,
        opacity,
        dashSize: 0.18,
        gapSize: 0.1,
        linewidth: 1, // ignored on most platforms but fine
        depthWrite: false,
      }),
    [color, opacity]
  );
  // Convert circle to a line (remove fill)
  useEffect(() => {
    geom.rotateX(Math.PI / 2);
    // convert to line geometry
    const pts = geom.getAttribute("position").array;
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(new Float32Array(pts), 3));
    ref.current.geometry = g;
    ref.current.computeLineDistances();
  }, [geom]);

  useFrame((_, dt) => {
    if (!ref.current) return;
    ref.current.rotation.z -= speed * dt * 0.2;
  });

  return <line ref={ref} renderOrder={3} material={mat} />;
}

/* ---------------------- Auxiliary tilted rings ------------- */
function TechRings({ color = "#60A5FA" }) {
  const m = useMemo(
    () =>
      new THREE.MeshBasicMaterial({
        color,
        transparent: true,
        opacity: 0.22,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
      }),
    [color]
  );
  return (
    <group renderOrder={2}>
      <mesh rotation={[Math.PI / 2.8, 0.3, 0]} position={[0, 0, 0]}>
        <torusGeometry args={[1.24, 0.005, 8, 180]} />
        <primitive object={m} attach="material" />
      </mesh>
      <mesh rotation={[Math.PI / 2.4, -0.6, 0.2]} position={[0, 0, 0]}>
        <torusGeometry args={[1.18, 0.005, 8, 160]} />
        <primitive object={m} attach="material" />
      </mesh>
    </group>
  );
}

/* ---------------------- The orb (tech rebuild) ------------- */
function HolographicCoreOrb({
  theme,
  quality = "auto",
  hexTextureUrl = "", // optional
  motionEnabled = true,
}) {
  const coreRef = useRef();
  const nucleusRef = useRef();
  const rimRef = useRef();

  // Geometry detail
  const seg = useMemo(() => {
    if (quality === "high") return 96;
    if (quality === "low") return 32;
    const isMobile = typeof navigator !== "undefined" && /Mobi|Android/i.test(navigator.userAgent);
    return isMobile ? 40 : 64;
  }, [quality]);

  // Animation: small pulse on core & nucleus. Shells keep fixed radii (no z-fight).
  useFrame(({ clock }, dt) => {
    if (!motionEnabled) return;
    const t = clock.getElapsedTime();
    const p = 1 + 0.02 * Math.sin(t * theme.pulse);
    if (coreRef.current) coreRef.current.scale.setScalar(p);
    if (nucleusRef.current) {
      const q = 1 + 0.06 * Math.sin(t * (theme.pulse * 1.15));
      nucleusRef.current.scale.setScalar(q);
      nucleusRef.current.rotation.y += 0.6 * dt;
      nucleusRef.current.rotation.x -= 0.35 * dt;
    }
    if (rimRef.current) rimRef.current.rotation.y -= 0.05 * dt;
  });

  useEffect(() => {
    if (coreRef.current) coreRef.current.renderOrder = 1;
    if (rimRef.current) rimRef.current.renderOrder = 3;
  }, []);

  return (
    <>
      {/* Glassy plasma core (cool, techy) */}
      <mesh ref={coreRef}>
        <sphereGeometry args={[1.0, seg, seg]} />
        {/* Slightly glassy + emissive plasma with subtle distortion */}
        <MeshDistortMaterial
          color={theme.core}
          distort={theme.distort}
          speed={motionEnabled ? theme.pulse : 0}
          transparent
          opacity={0.22}
          roughness={0.1}
          metalness={0.65}
          emissive={theme.core}
          emissiveIntensity={0.18}
          depthWrite={true}
          depthTest={true}
        />
      </mesh>

      {/* Energy nucleus (icosahedral spark) */}
      <mesh ref={nucleusRef} renderOrder={1} scale={0.3}>
        <icosahedronGeometry args={[0.42, 0]} />
        <meshStandardMaterial
          color={theme.accent}
          emissive={theme.accent}
          emissiveIntensity={0.9}
          metalness={0.2}
          roughness={0.35}
          transparent
          opacity={0.8}
          depthWrite={false}
        />
      </mesh>

      {/* Optional hex shell */}
      {hexTextureUrl ? <HexShell url={hexTextureUrl} radius={1.10} /> : null}

      {/* Tech wireframe grid (thin, rotating) */}
      <GridShell radius={1.12} color={theme.glow} opacity={0.22} speed={0.08} segments={48} />

      {/* Slim Fresnel rim (no thick halo) */}
      <mesh ref={rimRef}>
        <sphereGeometry args={[1.18, seg, seg]} />
        <FresnelMaterial color={theme.glow} intensity={0.42} power={2.1} bias={0.06} />
      </mesh>

      {/* Tilted additive rings (equatorial dashed ring removed) */}
      <TechRings color={theme.glow} />
    </>
  );
}
/* ---------------------- Smart DPR -------------------------- */
function DprManager({ maxDpr = 2 }) {
  const { gl } = useThree();
  useEffect(() => {
    const dpr = Math.min(
      maxDpr,
      typeof window !== "undefined" ? window.devicePixelRatio || 1 : 1
    );
    gl.setPixelRatio(dpr);
    gl.shadowMap.enabled = false;
    gl.sortObjects = true;
  }, [gl, maxDpr]);
  return null;
}

/* ---------------------- Public component ------------------- */
export default function NovaOrb3D({
  state = "idle",     // "idle" | "listening" | "thinking" | "speaking"
  size = 400,         // px
  showText = true,
  text = "NOVA",
  hexTextureUrl = "", // optional path
  quality = "auto",   // "auto" | "high" | "low"
  bloom = true,
}) {
  const reduced = useReducedMotion();
  const pageVisibleRef = usePageVisible();

  const theme = STATE_THEMES[state] ?? STATE_THEMES.idle;
  const motionEnabled = !reduced && pageVisibleRef.current;

  const containerStyle = useMemo(
    () => ({ width: size, height: size, position: "relative", zIndex: 10 }),
    [size]
  );

  return (
    <div style={containerStyle}>
      <Canvas
        camera={{ position: [0, 0, 7.5], fov: 50 }}
        style={{ background: "transparent" }}
        gl={{
          antialias: true,
          alpha: true,
          powerPreference: "high-performance",
          stencil: false,
          depth: true,
        }}
        onCreated={({ gl }) => gl.setClearColor(0x000000, 0)}
        frameloop="always"
      >
        <DprManager maxDpr={motionEnabled ? 2 : 1.25} />

        {/* Lighting */}
        <ambientLight intensity={1.0} />
        <directionalLight position={[4, 6, 8]} intensity={0.9} color={theme.core} />
        <directionalLight position={[-4, -3, -6]} intensity={0.4} color={theme.glow} />

        {/* Post bloom (subtle) */}
        {!reduced && bloom && (
          <EffectComposer multisampling={0}>
            <Bloom
              intensity={theme.bloom}
              radius={0.42}
              luminanceThreshold={0.45}
              luminanceSmoothing={0.16}
            />
          </EffectComposer>
        )}

        {/* Orb & Text */}
        <HolographicCoreOrb
          theme={theme}
          quality={quality}
          hexTextureUrl={hexTextureUrl}
          motionEnabled={motionEnabled}
        />
        {showText && <OrbitingNovaRing text={text} color={theme.text} />}
      </Canvas>
    </div>
  );
}
