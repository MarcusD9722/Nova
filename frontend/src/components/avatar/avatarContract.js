export const DEFAULT_MORPH_TARGETS = Object.freeze({
  // Aliases cover ARKit (facecap-style, _L/_R), Ready Player Me, and Oculus names.
  jawOpen: ["jawOpen", "JawOpen", "mouthOpen", "viseme_aa"],
  blinkLeft: ["eyeBlinkLeft", "EyeBlinkLeft", "blink_L", "eyeBlink_L"],
  blinkRight: ["eyeBlinkRight", "EyeBlinkRight", "blink_R", "eyeBlink_R"],
  blink: ["blink", "Blink", "eyesClosed"],
  smile: ["mouthSmile", "MouthSmile", "smile", "mouthSmileLeft", "mouthSmileRight", "mouthSmile_L", "mouthSmile_R"],
  focus: ["browInnerUp", "BrowInnerUp"],
  visemeA: ["viseme_A", "viseme_aa", "mouthA", "A"],
  visemeE: ["viseme_E", "viseme_EE", "mouthE", "E", "mouthStretch_L"],
  visemeI: ["viseme_I", "viseme_IH", "mouthI", "I"],
  visemeO: ["viseme_O", "viseme_oh", "mouthO", "O", "mouthFunnel"],
  visemeU: ["viseme_U", "viseme_ou", "mouthU", "U", "mouthPucker"],
});

export const NOVA_AVATAR_MODEL_CONTRACT = Object.freeze({
  states: ["idle", "listening", "thinking", "speaking"],
  facialInputs: ["jawOpen", "viseme", "expression", "blink", "smile"],
  supportedVisemes: ["A", "E", "I", "O", "U", "sil"],
  defaultMorphTargets: DEFAULT_MORPH_TARGETS,
});
