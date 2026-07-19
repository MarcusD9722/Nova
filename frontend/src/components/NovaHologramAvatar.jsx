import React, { Component, Suspense, useEffect } from "react";
import { Canvas } from "@react-three/fiber";
// Subpath imports keep the dev bundle small (full drei = 3.7MB, which the
// local antivirus kills mid-transfer — see vite.config.js).
import { AdaptiveDpr } from "@react-three/drei/core/AdaptiveDpr";
import { PerspectiveCamera } from "@react-three/drei/core/PerspectiveCamera";
import { Bloom, EffectComposer } from "@react-three/postprocessing";
import EnergyCore from "./avatar/EnergyCore";
import GlbAvatarSlot from "./avatar/GlbAvatarSlot";
import AvatarLights from "./avatar/AvatarLights";
import AvatarRings from "./avatar/AvatarRings";
import { DEFAULT_MORPH_TARGETS } from "./avatar/avatarContract";
import useAvatarState from "../hooks/useAvatarState";
import "./NovaHologramAvatar.css";

const bundledAvatarAssets = import.meta.glob("../assets/avatar/*.glb", {
  eager: true,
  query: "?url",
  import: "default",
});

const DEFAULT_MODEL_URL = bundledAvatarAssets["../assets/avatar/nova.glb"] || null;
let warnedAboutMissingDefaultModel = false;

class AvatarModelBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { failedUrl: null };
  }

  static getDerivedStateFromError() {
    return { failedUrl: "failed" };
  }

  componentDidCatch(error) {
    console.warn("[Nova Avatar] GLB model could not be loaded; using the hologram fallback.", error?.message || error);
  }

  componentDidUpdate(previousProps) {
    if (previousProps.modelUrl !== this.props.modelUrl && this.state.failedUrl) {
      this.setState({ failedUrl: null });
    }
  }

  render() {
    return this.state.failedUrl ? this.props.fallback : this.props.children;
  }
}

function AvatarModel({ avatarState, modelUrl, ModelComponent, modelProps, morphTargetMap, onMorphTargetsDetected }) {
  // Fallback is a minimal energy core — the procedural bust avatar is retired.
  const fallback = <EnergyCore avatarState={avatarState} />;

  if (ModelComponent) {
    return <ModelComponent avatarState={avatarState} morphTargetMap={morphTargetMap} {...modelProps} />;
  }

  if (!modelUrl) return fallback;

  return (
    <AvatarModelBoundary modelUrl={modelUrl} fallback={fallback}>
      <Suspense fallback={fallback}>
        <GlbAvatarSlot
          modelUrl={modelUrl}
          avatarState={avatarState}
          morphTargetMap={morphTargetMap}
          onMorphTargetsDetected={onMorphTargetsDetected}
          {...modelProps}
        />
      </Suspense>
    </AvatarModelBoundary>
  );
}

export default function NovaHologramAvatar({
  state,
  status,
  voiceState,
  isListening = false,
  isSpeaking = false,
  isThinking = false,
  size = 510,
  micLevel = 0,
  audioLevel,
  ttsPlaying = false,
  lipSync = null,
  modelUrl,
  modelComponent: ModelComponent = null,
  modelProps = {},
  morphTargetMap = DEFAULT_MORPH_TARGETS,
  onMorphTargetsDetected,
  className = "",
  style,
  ...props
}) {
  const normalizedPropState = isSpeaking
    ? "speaking"
    : isListening
      ? "listening"
      : isThinking
        ? "thinking"
        : state || voiceState || status || "idle";
  const resolvedModelUrl = modelUrl === undefined ? DEFAULT_MODEL_URL : modelUrl;
  const resolvedAudioLevel = audioLevel ?? micLevel;
  const avatarState = useAvatarState({
    state: normalizedPropState,
    micLevel: resolvedAudioLevel,
    ttsPlaying: ttsPlaying || isSpeaking,
    lipSync,
  });
  const safeSize = Math.max(260, Number(size) || 510);

  useEffect(() => {
    if (resolvedModelUrl || ModelComponent || warnedAboutMissingDefaultModel) return;
    warnedAboutMissingDefaultModel = true;
    console.warn("[Nova Avatar] src/assets/avatar/nova.glb was not found; using the hologram fallback.");
  }, [ModelComponent, resolvedModelUrl]);

  return (
    <div
      className={`nova-hologram-avatar nova-hologram-avatar--${avatarState.state} ${className}`.trim()}
      data-avatar-state={avatarState.state}
      data-avatar-source={resolvedModelUrl || ModelComponent ? "model" : "fallback"}
      style={{ width: safeSize, height: safeSize, ...style }}
      role="img"
      aria-label={`Nova holographic avatar, ${avatarState.state}`}
      {...props}
    >
      <div className="nova-hologram-avatar__aura" aria-hidden="true" />
      <Canvas
        className="nova-hologram-avatar__canvas"
        dpr={[1, 1.65]}
        gl={{ alpha: true, antialias: true, powerPreference: "high-performance", preserveDrawingBuffer: true }}
        shadows={false}
      >
        <PerspectiveCamera makeDefault position={[0, 0.14, 4.05]} fov={39} near={0.1} far={40} />
        <AvatarLights state={avatarState.state} />
        <AvatarRings avatarState={avatarState} />

        <AvatarModel
          avatarState={avatarState}
          modelUrl={resolvedModelUrl}
          ModelComponent={ModelComponent}
          modelProps={modelProps}
          morphTargetMap={morphTargetMap}
          onMorphTargetsDetected={onMorphTargetsDetected}
        />

        <EffectComposer multisampling={0}>
          <Bloom
            intensity={avatarState.state === "speaking" ? 0.72 : avatarState.state === "listening" ? 0.6 : 0.46}
            luminanceThreshold={0.5}
            luminanceSmoothing={0.34}
            mipmapBlur
          />
        </EffectComposer>
        <AdaptiveDpr pixelated />
      </Canvas>

      <div className="nova-hologram-avatar__scanlines" aria-hidden="true" />
      <div className="nova-hologram-avatar__distortion" aria-hidden="true" />
      <div className="nova-hologram-avatar__projector" aria-hidden="true">
        <span />
        <span />
        <span />
      </div>
    </div>
  );
}
