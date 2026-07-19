import React, { useEffect, useMemo, useRef } from "react";
import { useFrame, useThree } from "@react-three/fiber";
// Subpath import: pulling all of drei creates a 3.7MB dev bundle that
// Norton's network filter kills mid-transfer (blank app). Gltf.js alone is tiny.
import { useGLTF } from "@react-three/drei/core/Gltf";
import { clone } from "three/examples/jsm/utils/SkeletonUtils.js";
import { KTX2Loader } from "three/examples/jsm/loaders/KTX2Loader.js";
import * as THREE from "three";
import { DEFAULT_MORPH_TARGETS } from "./avatarContract";
import { createJawSyncState, jawTargetFor, smoothJaw } from "../../voice/lipSync";

// Shared KTX2 transcoder (models like facecap ship KTX2-compressed textures).
// Transcoder wasm lives in public/basis/ so it works offline and in Electron.
let _ktx2Loader = null;
function getKtx2Loader(gl) {
  if (!_ktx2Loader) {
    _ktx2Loader = new KTX2Loader().setTranscoderPath("basis/");
  }
  if (gl) _ktx2Loader.detectSupport(gl);
  return _ktx2Loader;
}

const HOLOGRAM_PURPLE = new THREE.Color("#8b5cf6");
const HOLOGRAM_BLUE = new THREE.Color("#52a8ff");
const VISEME_KEYS = ["visemeA", "visemeE", "visemeI", "visemeO", "visemeU"];

function setMorph(meshes, names, value, damping = 0.18) {
  meshes.forEach((mesh) => {
    names.forEach((name) => {
      const index = mesh.morphTargetDictionary?.[name];
      if (index == null) return;
      mesh.morphTargetInfluences[index] += (value - mesh.morphTargetInfluences[index]) * damping;
    });
  });
}

function normalizeViseme(viseme) {
  const value = String(viseme || "sil").trim().toUpperCase();
  return ["A", "E", "I", "O", "U"].includes(value) ? value : "SIL";
}

function prepareModel(sourceScene, targetHeight) {
  const instance = clone(sourceScene);
  const morphMeshes = [];
  const hologramMaterials = [];
  const eyeMaterials = [];
  const focusMaterials = [];

  // Authored hologram avatars (built by our Blender pipeline) arrive already
  // materialized — keep their materials, just index them for state glow.
  const isAuthored = Boolean(instance.getObjectByName("Nova_Root") || instance.getObjectByName("Nova_Face"));

  if (isAuthored) {
    instance.traverse((node) => {
      if (node.isCamera || node.isLight) node.visible = false;
      if (!node.isMesh) return;
      if (node.morphTargetDictionary && node.morphTargetInfluences) morphMeshes.push(node);
      const materials = (Array.isArray(node.material) ? node.material : [node.material]).filter(Boolean);
      materials.forEach((material) => {
        if (material.emissive && material.emissiveIntensity > 0) {
          material.userData.baseEmissive = material.emissiveIntensity;
          hologramMaterials.push(material);
          if (/Eye/i.test(material.name)) eyeMaterials.push(material);
        }
        if (material.transparent) material.depthWrite = material.opacity > 0.6;
      });
      node.castShadow = false;
      node.receiveShadow = false;
    });

    instance.updateMatrixWorld(true);
    const bounds = new THREE.Box3().setFromObject(instance);
    const dimensions = bounds.getSize(new THREE.Vector3());
    const center = bounds.getCenter(new THREE.Vector3());
    const normalizationScale = dimensions.y > 0.001 ? targetHeight / dimensions.y : 1;
    const offset = center.multiplyScalar(-normalizationScale);
    return { instance, morphMeshes, hologramMaterials, eyeMaterials, focusMaterials, normalizationScale, offset, isAuthored };
  }

  instance.traverse((node) => {
    if (!node.isMesh) return;

    if (node.morphTargetDictionary && node.morphTargetInfluences) morphMeshes.push(node);

    const sourceMaterials = Array.isArray(node.material) ? node.material : [node.material];
    const clonedMaterials = sourceMaterials.filter(Boolean).map((sourceMaterial) => {
      const material = sourceMaterial.clone();
      const nodeName = String(node.name || "").toLowerCase();
      const isEye = /eye|iris|pupil/.test(nodeName);
      const isTeeth = /teeth|tongue|mouth/.test(nodeName);
      const isFocus = /forehead|circuit|temple|emissive/.test(nodeName);

      // Hologram treatment that PRESERVES facial detail: tint gently, keep
      // depth writes on (otherwise teeth/eyes render through the skin), and
      // let the texture carry the features — the glow comes from bloom,
      // scanlines, and rim light rather than flat emissive wash.
      material.transparent = true;
      material.opacity = Math.min(Number.isFinite(material.opacity) ? material.opacity : 1, isEye ? 1 : 0.94);
      material.depthWrite = true;
      material.side = THREE.FrontSide;

      if (material.color) material.color.lerp(isEye ? HOLOGRAM_BLUE : HOLOGRAM_PURPLE, isEye ? 0.18 : 0.38);
      if (material.emissive) {
        material.emissive.copy(isEye ? HOLOGRAM_BLUE : HOLOGRAM_PURPLE);
        material.emissiveIntensity = isEye ? 0.55 : isTeeth ? 0.08 : 0.2;
      }
      if ("roughness" in material) material.roughness = Math.min(material.roughness ?? 0.5, 0.42);
      if ("metalness" in material) material.metalness = Math.max(material.metalness ?? 0, 0.08);

      if (!isTeeth) hologramMaterials.push(material);
      if (isEye) eyeMaterials.push(material);
      if (isFocus) focusMaterials.push(material);
      return material;
    });

    node.material = Array.isArray(node.material) ? clonedMaterials : clonedMaterials[0];
    node.castShadow = false;
    node.receiveShadow = false;
  });

  instance.updateMatrixWorld(true);
  const bounds = new THREE.Box3().setFromObject(instance);
  const dimensions = bounds.getSize(new THREE.Vector3());
  const center = bounds.getCenter(new THREE.Vector3());
  const normalizationScale = dimensions.y > 0.001 ? targetHeight / dimensions.y : 1;
  const offset = center.multiplyScalar(-normalizationScale);
  offset.y -= 0.1;

  return { instance, morphMeshes, hologramMaterials, eyeMaterials, focusMaterials, normalizationScale, offset, isAuthored: false };
}

export default function GlbAvatarSlot({
  modelUrl,
  avatarState,
  morphTargetMap = DEFAULT_MORPH_TARGETS,
  position = [0, -0.02, 0],
  rotation = [0, 0, 0],
  scale = 1,
  targetHeight = 3.4,
  onMorphTargetsDetected,
}) {
  const gl = useThree((state) => state.gl);
  const { scene, animations } = useGLTF(modelUrl, true, true, (loader) => {
    loader.setKTX2Loader(getKtx2Loader(gl));
  });
  const prepared = useMemo(() => prepareModel(scene, targetHeight), [scene, targetHeight]);
  const motionRoot = useRef(null);
  const jawSync = useRef(createJawSyncState(0));

  // Animation clips (authored avatars): ambient loops always run; state clips
  // crossfade with avatarState; Speaking/Blink stay real-time (morph-driven).
  const mixerRef = useRef(null);
  const actionsRef = useRef({});
  const activeStateClipRef = useRef(null);

  useEffect(() => {
    if (!animations?.length) return undefined;
    const mixer = new THREE.AnimationMixer(prepared.instance);
    const actions = {};
    animations.forEach((clip) => {
      actions[clip.name] = mixer.clipAction(clip);
    });
    ["IdleHover", "HUDRotate", "EyeGlowPulse"].forEach((name) => {
      const action = actions[name];
      if (action) action.setLoop(THREE.LoopRepeat).play();
    });
    mixerRef.current = mixer;
    actionsRef.current = actions;
    return () => {
      mixer.stopAllAction();
      mixer.uncacheRoot(prepared.instance);
      mixerRef.current = null;
      actionsRef.current = {};
      activeStateClipRef.current = null;
    };
  }, [animations, prepared.instance]);

  useEffect(() => {
    if (!onMorphTargetsDetected) return;
    const detected = prepared.morphMeshes.map((mesh) => ({
      mesh: mesh.name || "unnamed-mesh",
      targets: Object.keys(mesh.morphTargetDictionary || {}),
    }));
    onMorphTargetsDetected(detected);
  }, [onMorphTargetsDetected, prepared.morphMeshes]);

  useEffect(() => () => {
    prepared.hologramMaterials.forEach((material) => material.dispose());
  }, [prepared.hologramMaterials]);

  useFrame(({ clock }, delta) => {
    const t = clock.getElapsedTime();
    const speaking = avatarState.state === "speaking";
    const listening = avatarState.state === "listening";
    const thinking = avatarState.state === "thinking";
    const motion = avatarState.reducedMotion ? 0 : avatarState.config.motion;

    // ── Clip playback (authored avatars) ─────────────────────────────────
    const mixer = mixerRef.current;
    if (mixer) {
      const actions = actionsRef.current;

      // Crossfade the state clip (Listening/Thinking hold poses).
      const desired = listening ? "Listening" : thinking ? "Thinking" : null;
      if (activeStateClipRef.current !== desired) {
        const prev = actions[activeStateClipRef.current];
        if (prev) prev.fadeOut(0.35);
        const next = actions[desired];
        if (next) next.reset().setLoop(THREE.LoopRepeat).fadeIn(0.35).play();
        activeStateClipRef.current = desired;
      }

      // Waveform pulses only while she speaks.
      const wave = actions.WaveformPulse;
      if (wave) {
        if (speaking && !wave.isRunning()) wave.reset().setLoop(THREE.LoopRepeat).fadeIn(0.2).play();
        if (!speaking && wave.isRunning()) wave.fadeOut(0.4);
      }

      // One-shot Happy on smile expressions.
      if (avatarState.facial.expression === "smile") {
        const happy = actions.Happy;
        if (happy && !happy.isRunning()) {
          happy.reset().setLoop(THREE.LoopOnce).play();
        }
      }

      mixer.update(delta);
    }

    // Lip sync: follow the real TTS waveform when audio analysis is live,
    // fall back to a gentle procedural flap only while audio is actually
    // playing, and settle to closed otherwise (shared with HolographicBust).
    const jawTarget = jawTargetFor({
      externalJaw: avatarState.facial.jawOpen,
      speaking,
      audioEnergy: avatarState.audioEnergy,
      time: t,
      fallbackAmplitude: 0.48,
    });
    const jaw = smoothJaw(jawSync.current, jawTarget);
    const blinkPhase = t % 4.8;
    const blink = avatarState.facial.blink ?? (blinkPhase > 4.62 ? Math.sin(((blinkPhase - 4.62) / 0.18) * Math.PI) : 0);
    const activeViseme = normalizeViseme(avatarState.facial.viseme);

    if (motionRoot.current) {
      motionRoot.current.position.y = position[1] + Math.sin(t * 1.05) * 0.045 * motion;
      motionRoot.current.rotation.y = rotation[1] + Math.sin(t * 0.43) * 0.055 * motion;
      const breath = 1 + Math.sin(t * 1.34) * 0.008 * motion;
      motionRoot.current.scale.set(scale * breath, scale * (1 + (breath - 1) * 0.55), scale * breath);
    }

    // Update these aliases in avatarContract.js when the production model's real
    // morph target names are known. Missing targets are intentionally ignored.
    setMorph(prepared.morphMeshes, morphTargetMap.jawOpen || [], jaw, 1);
    setMorph(prepared.morphMeshes, morphTargetMap.blinkLeft || [], blink, 0.34);
    setMorph(prepared.morphMeshes, morphTargetMap.blinkRight || [], blink, 0.34);
    setMorph(prepared.morphMeshes, morphTargetMap.blink || [], blink, 0.34);
    setMorph(prepared.morphMeshes, morphTargetMap.smile || [], avatarState.facial.expression === "smile" ? 0.62 : 0);
    setMorph(prepared.morphMeshes, morphTargetMap.focus || [], thinking ? 0.34 : 0);
    VISEME_KEYS.forEach((key) => setMorph(prepared.morphMeshes, morphTargetMap[key] || [], key === `viseme${activeViseme}` ? 0.72 : 0, 0.26));

    if (prepared.isAuthored) {
      // Authored materials keep their designed strengths; state just scales them.
      const glowMult = speaking ? 1.35 : listening ? 1.18 : thinking ? 1.08 : 1.0;
      prepared.hologramMaterials.forEach((material) => {
        const base = material.userData.baseEmissive ?? material.emissiveIntensity;
        material.emissiveIntensity = THREE.MathUtils.lerp(material.emissiveIntensity, base * glowMult, delta * 4);
      });
      prepared.eyeMaterials.forEach((material) => {
        const base = material.userData.baseEmissive ?? 1;
        material.emissiveIntensity = base * (listening ? 1.3 : 1.0) + Math.sin(t * 2.1) * 0.12;
      });
    } else {
      const baseGlow = speaking ? 0.42 : listening ? 0.34 : 0.2;
      prepared.hologramMaterials.forEach((material) => {
        if (material.emissive) material.emissiveIntensity = THREE.MathUtils.lerp(material.emissiveIntensity, baseGlow, delta * 4);
      });
      prepared.eyeMaterials.forEach((material) => {
        if (material.emissive) material.emissiveIntensity = (listening ? 1.15 : 0.7) + Math.sin(t * 2.1) * 0.08;
      });
    }
    prepared.focusMaterials.forEach((material) => {
      if (material.emissive) material.emissiveIntensity = thinking ? 2.8 + Math.sin(t * 4) * 0.5 : baseGlow;
    });
  });

  return (
    <group ref={motionRoot} position={position} rotation={rotation} scale={scale}>
      <primitive object={prepared.instance} scale={prepared.normalizationScale} position={prepared.offset} />
    </group>
  );
}
