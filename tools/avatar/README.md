# Nova Hologram Avatar — build pipeline

`frontend/src/assets/avatar/nova.glb` (a.k.a. **Nova_Hologram_Avatar.glb**) is
generated procedurally by Blender Python around a realistic scanned face
(three.js "facecap", 52 ARKit shape keys).

## Contents of the GLB

- Named objects: `Nova_Root`, `Head_Root`, `Nova_Face`, `Nova_Eye_L/R`,
  `Nova_Teeth`, `Nova_Hair`, `EyeGlow_L/R`, `Bust_*`, `HUD_*`,
  `Waveform_Root` + 48 bars, `Nova_Camera` (front view)
- Named animation clips: `IdleHover`, `Blink`, `Speaking`, `Listening`,
  `Thinking`, `Happy`, `Alert`, `EyeGlowPulse`, `HUDRotate`, `WaveformPulse`
- Materials: `NovaHolo_*` (purple/blue/gold emissive hologram set with a
  packed scanline/circuit emissive texture)

The frontend (`GlbAvatarSlot.jsx`) detects `Nova_Root`, keeps the authored
materials, loops IdleHover/HUDRotate/EyeGlowPulse, crossfades
Listening/Thinking with assistant state, pulses the waveform while speaking,
and drives `jawOpen`/blinks in real time from the TTS audio analyser
(the `Speaking`/`Blink` clips exist for players without real-time audio).

## Regenerate

```powershell
# One-time: a venv with Blender-as-module
python -m venv bpyenv
bpyenv\Scripts\pip install bpy

# facecap_clean.glb = decompressed face (see decompress_face.mjs; needs
# `npm i @gltf-transform/core @gltf-transform/extensions @gltf-transform/functions meshoptimizer`)
node decompress_face.mjs facecap.glb facecap_clean.glb

bpyenv\Scripts\python build_nova_avatar.py facecap_clean.glb Nova_Hologram_Avatar.glb
Copy-Item Nova_Hologram_Avatar.glb ..\..\frontend\src\assets\avatar\nova.glb
```

Tweak knobs at the top of `build_nova_avatar.py` (palette, head size, hair
density/length, HUD radii) and re-run.

## Portrait mode (photo-projected face)

Pass a front-facing portrait as arg 3 and Nova's face becomes that image,
planar-projected onto the front polygons and aligned by eye/mouth landmarks
(the violet hologram skin remains on the sides; 3D eyeballs are replaced by
the painted eyes + EyeGlow discs; all morphs/lip-sync still work):

```powershell
bpyenv\Scripts\python build_nova_avatar.py facecap_clean.glb out.glb nova_reference.png renders\
```

- Landmark fractions of the image (env vars): `NOVA_PORTRAIT_EYE_V` (eye line
  height from bottom, default 0.565), `NOVA_PORTRAIT_MOUTH_V` (default 0.395),
  `NOVA_PORTRAIT_EYE_SPAN` (eye distance / width, default 0.205).
- Pass `@test` as the portrait to render calibration crosshairs instead.
- Arg 4 (optional) = a directory for Cycles verification renders
  (`front.png`, `jaw_open.png`, `blink.png`).
