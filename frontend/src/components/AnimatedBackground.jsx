import React, { useMemo, useRef, useEffect } from "react";
import { Canvas, useFrame } from "@react-three/fiber";
import * as THREE from "three";
// If you want bloom back later, uncomment these and the composer below
// import { EffectComposer, Bloom } from "@react-three/postprocessing";

/* ---------- Cursor projector (inside Canvas) ---------- */
function CursorProjector({ zPlane = 0, outRef }) {
  useFrame(({ camera, size }) => {
    if (!outRef?.current) return;
    const { ndc, world } = outRef.current;
    const mx = outRef.current._clientX ?? size.width / 2;
    const my = outRef.current._clientY ?? size.height / 2;

    const nx = (mx / size.width) * 2 - 1;
    const ny = -(my / size.height) * 2 + 1;
    ndc.set(nx, ny);

    const ndcVec = new THREE.Vector3(nx, ny, 0.5);
    const w = ndcVec.clone().unproject(camera);
    const dir = w.sub(camera.position).normalize();
    const t = (zPlane - camera.position.z) / dir.z;
    world.copy(camera.position).add(dir.multiplyScalar(t));
  });

  useEffect(() => {
    if (!outRef?.current) return;
    const setXY = (x, y) => {
      outRef.current._clientX = x;
      outRef.current._clientY = y;
    };
    const onMouse = (e) => setXY(e.clientX, e.clientY);
    const onTouch = (e) => {
      if (e.touches && e.touches[0]) setXY(e.touches[0].clientX, e.touches[0].clientY);
    };
    window.addEventListener("mousemove", onMouse, { passive: true });
    window.addEventListener("touchmove", onTouch, { passive: true });
    return () => {
      window.removeEventListener("mousemove", onMouse);
      window.removeEventListener("touchmove", onTouch);
    };
  }, [outRef]);

  return null;
}

/* ---------- Shockwave trigger (click/touch) ---------- */
function ShockProjector({ cursorRef, shockRef }) {
  useEffect(() => {
    if (!shockRef?.current) return;
    const arm = () => {
      const p = cursorRef?.current?.world ?? new THREE.Vector3();
      shockRef.current.center.copy(p);
      shockRef.current.time = performance.now() / 1000;
      shockRef.current.active = true;
    };
    window.addEventListener("mousedown", arm, { passive: true });
    window.addEventListener("touchstart", arm, { passive: true });
    return () => {
      window.removeEventListener("mousedown", arm);
      window.removeEventListener("touchstart", arm);
    };
  }, [cursorRef, shockRef]);
  return null;
}

/* ---------- Optional: visualize influence radius ---------- */
function CursorDebugRing({ cursorRef, radius = 6, color = "#22d3ee", opacity = 0.35 }) {
  const lineRef = useRef();
  const geom = useMemo(() => {
    const pts = [];
    for (let i = 0; i <= 64; i++) {
      const a = (i / 64) * Math.PI * 2;
      pts.push(new THREE.Vector3(Math.cos(a) * radius, Math.sin(a) * radius, 0));
    }
    return new THREE.BufferGeometry().setFromPoints(pts);
  }, [radius]);

  useFrame(() => {
    if (!lineRef.current || !cursorRef?.current) return;
    const c = cursorRef.current.world;
    lineRef.current.position.set(c.x, c.y, 0);
  });

  return (
    <line ref={lineRef} renderOrder={10}>
      <primitive object={geom} attach="geometry" />
      <lineBasicMaterial color={color} transparent opacity={opacity} depthWrite={false} />
    </line>
  );
}

/* ---------- GPU particle layer (stable, interactive) ---------- */
function SwarmLayer({
  count = 900,
  radius = 14,
  color = "#48eaff",
  speed = 0.45,        // ↓ slower
  swirl = 0.10,        // ↓ slower spin
  wobble = 0.8,        // ↓ a touch calmer
  sizeMin = 0.10,
  sizeMax = 0.26,

  // Interaction:
  interactionMode = "repel", // "repel" | "attract" | "orbit" | "highlight"
  cursorRadius = 6.5,
  cursorStrength = 3.0,
  cursorRef,

  // Shockwave:
  shockRef,
  shockSpeed = 5.0,
  shockBaseRadius = 0.0,
  shockWidth = 1.0,
  shockStrength = 1.8,
  shockDecay = 2.0,
}) {
  const { positions, sizes, seeds } = useMemo(() => {
    const pos = new Float32Array(count * 3);
    const siz = new Float32Array(count);
    const sed = new Float32Array(count);

    for (let i = 0; i < count; i++) {
      const u = Math.random();
      const v = Math.random();
      const theta = 2 * Math.PI * u;
      const phi = Math.acos(2 * v - 1);
      const r = radius * (0.75 + 0.25 * Math.random());
      const x = r * Math.sin(phi) * Math.cos(theta);
      const y = r * Math.sin(phi) * Math.sin(theta);
      const z = r * Math.cos(phi);
      pos[i * 3 + 0] = x;
      pos[i * 3 + 1] = y;
      pos[i * 3 + 2] = z;

      siz[i] = sizeMin + Math.random() * (sizeMax - sizeMin);
      sed[i] = Math.random() * 1000 + i * 0.137;
    }
    return { positions: pos, sizes: siz, seeds: sed };
  }, [count, radius, sizeMin, sizeMax]);

  const vertexShader = /* glsl */ `
    precision highp float;
    attribute float aSize;
    attribute float aSeed;

    uniform float uTime;
    uniform float uSpeed;
    uniform float uSwirl;
    uniform float uWobble;
    uniform float uRadius;

    uniform vec3  uCursor;
    uniform float uCursorRadius;
    uniform float uCursorStrength;
    uniform int   uMode; // 0=repel,1=attract,2=orbit,3=highlight

    uniform vec3  uShockCenter;
    uniform float uShockTime;
    uniform float uShockActive;
    uniform float uShockSpeed;
    uniform float uShockBaseRadius;
    uniform float uShockWidth;
    uniform float uShockStrength;
    uniform float uShockDecay;

    // 3D simplex noise (compact)
    vec3 mod289(vec3 x){return x - floor(x*(1.0/289.0))*289.0;}
    vec4 mod289(vec4 x){return x - floor(x*(1.0/289.0))*289.0;}
    vec4 permute(vec4 x){return mod289(((x*34.0)+1.0)*x);}
    vec4 taylorInvSqrt(vec4 r){return 1.7928429 - 0.8537347*r;}
    float snoise(vec3 v){
      const vec2 C=vec2(1.0/6.0,1.0/3.0);
      vec3 i=floor(v+dot(v,C.yyy)); vec3 x0=v-i+dot(i,C.xxx);
      vec3 g=step(x0.yzx,x0.xyz); vec3 l=1.0-g;
      vec3 i1=min(g.xyz,l.zxy); vec3 i2=max(g.xyz,l.zxy);
      vec3 x1=x0-i1+C.xxx, x2=x0-i2+2.0*C.xxx, x3=x0-1.0+3.0*C.xxx;
      i=mod289(i);
      vec4 p=permute(permute(permute(
        i.z+vec4(0.0,i1.z,i2.z,1.0))+i.y+vec4(0.0,i1.y,i2.y,1.0))+i.x+vec4(0.0,i1.x,i2.x,1.0));
      float n_=1.0/7.0; vec3 ns=n_*vec3(1.0,2.0,3.0)-vec3(0.0);
      vec4 j=p-49.0*floor(p*(1.0/7.0)*(1.0/7.0));
      vec4 x_=floor(j*(1.0/7.0)), y_=floor(j-7.0*x_);
      vec4 x=x_*(1.0/6.0)+(1.0/3.0), y=y_*(1.0/6.0)+(1.0/3.0);
      vec4 h=1.0-abs(x)-abs(y);
      vec4 b0=vec4(x.xy,y.xy), b1=vec4(x.zw,y.zw);
      vec4 s0=floor(b0)*2.0+1.0, s1=floor(b1)*2.0+1.0;
      vec4 sh=-step(h,vec4(0.0));
      vec4 a0=b0.xzyw+s0.xzyw*sh.xxyy, a1=b1.xzyw+s1.xzyw*sh.zzww;
      vec3 p0=vec3(a0.xy,h.x), p1=vec3(a1.xy,h.y), p2=vec3(a0.zw,h.z), p3=vec3(a1.zw,h.w);
      vec4 norm=taylorInvSqrt(vec4(dot(p0,p0),dot(p1,p1),dot(p2,p2),dot(p3,p3)));
      p0*=norm.x; p1*=norm.y; p2*=norm.z; p3*=norm.w;
      vec4 m=max(0.6-vec4(dot(x0,x0),dot(x1,x1),dot(x2,x2),dot(x3,x3)),0.0); m*=m;
      return 42.0*dot(m*m, vec4(dot(p0,x0),dot(p1,x1),dot(p2,x2),dot(p3,x3)));
    }

    varying float vAlpha;

    void main() {
      vec3 pos = position;

      // baseline swirl (slower)
      float ang = uSwirl * (uTime * uSpeed * 0.6 + aSeed * 0.01);
      float s = sin(ang), c = cos(ang);
      pos = vec3(c*pos.x + s*pos.z, pos.y, -s*pos.x + c*pos.z);

      // wobble (subtle)
      float n1 = snoise(pos * 0.12 + vec3(0.0, uTime * 0.12 * uSpeed, aSeed));
      float n2 = snoise(pos * 0.21 + vec3(uTime * 0.06 * uSpeed, aSeed, 0.0));
      vec3 wob = normalize(vec3(n1, n2, n1 - n2 + 0.2)) * uWobble;
      pos += wob;

      // cursor field
      vec3 toC = uCursor - pos;
      float d = length(toC);
      float influence = 1.0 - smoothstep(0.0, uCursorRadius, d);
      influence *= influence;
      float nearBoost = 1.0 / (0.22 + d * 0.8);
      float force = uCursorStrength * nearBoost;

      if (uMode == 0) { pos -= normalize(toC) * force * influence; }
      else if (uMode == 1) { pos += normalize(toC) * force * influence; }
      else if (uMode == 2) {
        vec3 perp = normalize(vec3(-toC.z, 0.0, toC.x));
        pos += perp * (force * 0.9) * influence;
        pos += normalize(toC) * (force * 0.15) * influence;
      }

      // shockwave ring
      if (uShockActive > 0.5) {
        float age = max(0.0, uTime - uShockTime);
        float ringR = uShockBaseRadius + age * uShockSpeed;
        vec3  toS = pos - uShockCenter;
        float distS = length(toS);
        float band = 1.0 - smoothstep(0.0, uShockWidth, abs(distS - ringR));
        float amp = uShockStrength * band * exp(-uShockDecay * age);
        if (distS > 0.0001) { pos += normalize(toS) * amp; }
      }

      // soft boundary
      float len = length(pos);
      if (len > uRadius * 1.15) pos *= (uRadius * 1.15) / len;

      // size + fade
      vec4 mvPosition = modelViewMatrix * vec4(pos, 1.0);
      float dist = length(mvPosition.xyz);
      float baseSize = aSize * (400.0 / dist);
      float pulse = 0.85 + 0.3 * sin(uTime * (1.0 * uSpeed) + aSeed);
      gl_PointSize = baseSize * pulse;

      vAlpha = smoothstep(uRadius * 1.25, uRadius * 0.2, len);
      if (uMode == 3) { vAlpha += 0.6 * influence; } // highlight

      gl_Position = projectionMatrix * mvPosition;
    }
  `;

  const fragmentShader = /* glsl */ `
    precision highp float;
    uniform vec3  uColor;
    varying float vAlpha;
    void main() {
      vec2 uv = gl_PointCoord - vec2(0.5);
      float d = length(uv);
      float a = smoothstep(0.5, 0.05, d);
      gl_FragColor = vec4(uColor, a * 0.85 * vAlpha);
    }
  `;

  const matRef = useRef();
  const groupRef = useRef();

  useFrame(({ clock }) => {
    const t = clock.getElapsedTime();

    // Even subtler fallback rotation (so overall spin feels slower)
    if (groupRef.current) {
      groupRef.current.rotation.y += 0.001;   // ↓ was 0.003
      groupRef.current.rotation.x += 0.0003;  // ↓ was 0.0008
    }

    if (!matRef.current) return;
    const u = matRef.current.uniforms;
    u.uTime.value = t;

    if (cursorRef?.current) {
      const c = cursorRef.current.world;
      u.uCursor.value.set(c.x, c.y, c.z);
    }
    // push interaction every frame
    u.uCursorRadius.value   = cursorRadius;
    u.uCursorStrength.value = cursorStrength;
    u.uMode.value =
      interactionMode === "repel" ? 0 :
      interactionMode === "attract" ? 1 :
      interactionMode === "orbit" ? 2 : 3;

    if (shockRef?.current) {
      const s = shockRef.current;
      u.uShockCenter.value.copy(s.center);
      u.uShockTime.value = s.time;
      const active = s.active && (t - s.time) < 2.5 ? 1.0 : 0.0;
      u.uShockActive.value = active;
      if (!active && s.active) s.active = false;
    }
  });

  return (
    <group ref={groupRef} frustumCulled={false}>
      <points renderOrder={1}>
        <bufferGeometry>
          <bufferAttribute attach="attributes-position" array={positions} count={count} itemSize={3} />
          <bufferAttribute attach="attributes-aSize" array={sizes} count={count} itemSize={1} />
          <bufferAttribute attach="attributes-aSeed" array={seeds} count={count} itemSize={1} />
        </bufferGeometry>
        <shaderMaterial
          ref={matRef}
          vertexShader={vertexShader}
          fragmentShader={fragmentShader}
          depthWrite={false}
          depthTest={true}
          transparent
          blending={THREE.AdditiveBlending}
          uniforms={{
            uTime:   { value: 0 },
            uSpeed:  { value: speed },
            uSwirl:  { value: swirl },
            uWobble: { value: wobble },
            uRadius: { value: radius },
            uColor:  { value: new THREE.Color(color) },

            uCursor:        { value: new THREE.Vector3(0, 0, 0) },
            uCursorRadius:  { value: cursorRadius },
            uCursorStrength:{ value: cursorStrength },
            uMode:          { value: 0 },

            uShockCenter:     { value: new THREE.Vector3(0,0,0) },
            uShockTime:       { value: 0 },
            uShockActive:     { value: 0 },
            uShockSpeed:      { value: shockSpeed },
            uShockBaseRadius: { value: shockBaseRadius },
            uShockWidth:      { value: shockWidth },
            uShockStrength:   { value: shockStrength },
            uShockDecay:      { value: shockDecay },
          }}
        />
      </points>
    </group>
  );
}

/* ---------- Full-screen background with 4 layers (more depth) ---------- */
export default function AnimatedBackground({ showDebugRing = false }) {
  const cursorRef = useRef({
    world: new THREE.Vector3(),
    ndc: new THREE.Vector2(0, 0),
    _clientX: undefined,
    _clientY: undefined,
  });

  const shockRef = useRef({
    active: false,
    time: 0,
    center: new THREE.Vector3(),
  });

  return (
    <div
      style={{
        position: "absolute",
        inset: 0,
        width: "100vw",
        height: "100vh",
        zIndex: 0,
        pointerEvents: "none",
        background: "radial-gradient(ellipse at 60% 40%, #171a1e 70%, #080c11 100%)",
      }}
    >
      <Canvas
        camera={{ position: [0, 0, 32], fov: 55 }}
        dpr={[1, 2]}
        gl={{ antialias: true, alpha: true, depth: true, powerPreference: "high-performance" }}
        frameloop="always"
      >
        <color attach="background" args={["#070b14"]} />
        <ambientLight intensity={1} />

        <CursorProjector zPlane={0} outRef={cursorRef} />
        <ShockProjector cursorRef={cursorRef} shockRef={shockRef} />
        {showDebugRing && <CursorDebugRing cursorRef={cursorRef} radius={6} />}

        {/* Deep background: very wide, gentle attract (NEW LAYER) */}
        <SwarmLayer
          count={1600}          // ↑ more depth particles
          radius={28}
          color="#1e90ff"
          speed={0.20}
          swirl={0.05}
          wobble={0.6}
          sizeMin={0.08}
          sizeMax={0.18}
          interactionMode="attract"
          cursorRef={cursorRef}
          cursorRadius={9.0}
          cursorStrength={1.0}
          shockRef={shockRef}
          shockStrength={0.9}
        />

        {/* Far: attract, wide & soft */}
        <SwarmLayer
          count={1200}         // ↑ was 800
          radius={20}
          color="#ffe872"
          speed={0.22}         // ↓ slower
          swirl={0.06}         // ↓ slower spin
          wobble={0.65}
          sizeMin={0.10}
          sizeMax={0.22}
          interactionMode="attract"
          cursorRef={cursorRef}
          cursorRadius={7.5}
          cursorStrength={1.4}
          shockRef={shockRef}
          shockStrength={1.1}
        />

        {/* Mid: repel, strongest */}
        <SwarmLayer
          count={2400}         // ↑ was 1400
          radius={14}
          color="#48eaff"
          speed={0.35}         // ↓ slower
          swirl={0.09}         // ↓ slower spin
          wobble={0.85}
          sizeMin={0.12}
          sizeMax={0.26}
          interactionMode="repel"
          cursorRef={cursorRef}
          cursorRadius={7.0}
          cursorStrength={3.0}
          shockRef={shockRef}
          shockStrength={1.6}
        />

        {/* Near: orbit for wrap-around feel */}
        <SwarmLayer
          count={800}          // ↑ was 420
          radius={9}
          color="#47fff2"
          speed={0.50}         // ↓ slower
          swirl={0.12}         // ↓ slower spin
          wobble={1.0}
          sizeMin={0.16}
          sizeMax={0.34}
          interactionMode="orbit"
          cursorRef={cursorRef}
          cursorRadius={6.0}
          cursorStrength={3.4}
          shockRef={shockRef}
          shockStrength={1.8}
        />

        {/* Re-enable if you want glow:
        <EffectComposer multisampling={0}>
          <Bloom luminanceThreshold={0.22} luminanceSmoothing={0.55} intensity={1.6} />
        </EffectComposer> */}
      </Canvas>
    </div>
  );
}
