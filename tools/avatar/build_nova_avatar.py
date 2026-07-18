"""Build Nova_Hologram_Avatar.glb — a holographic AI assistant bust.

Run with bpy-as-module (Blender 5.x):
    python build_nova_avatar.py <face_glb_in> <glb_out> [portrait_png] [renders_dir]

Optional portrait stage: planar-projects a front-facing portrait image onto
Nova_Face (front polygons only; the violet hologram skin remains elsewhere),
aligned so the image's eyes/mouth land on the geometry's eyes/mouth.
Pass "@test" as portrait_png to generate a calibration image (crosshairs at
the expected landmarks). Landmark fractions of the image can be tuned via env:
  NOVA_PORTRAIT_EYE_V (default 0.565)   height fraction of eye line (from bottom)
  NOVA_PORTRAIT_MOUTH_V (default 0.395) height fraction of mouth line
  NOVA_PORTRAIT_EYE_SPAN (default 0.205) eye-center distance / image width
If renders_dir is given, low-sample Cycles verification renders are written:
front.png, jaw_open.png, blink.png.

Scene (Z-up in Blender, exported Y-up glTF; front = -Y):
  Nova_Root
    Head_Root            (imported realistic face w/ 52 ARKit shape keys)
      Nova_Face / Nova_Teeth / Nova_Eye_L / Nova_Eye_R
      EyeGlow_L/R        (emissive pulse discs)
      Hair_Root          (procedural holographic bob, ~110 strands)
    Bust_Root            (glass neck/shoulders/chest shell + gold core)
    HUD_Root             (rings, ticks, arcs, particles — behind head)
    Waveform_Root        (48 emissive bars, pulse clip)

Named animation clips (NLA tracks; exported with NLA_TRACKS mode):
  IdleHover, Blink, Speaking, Listening, Thinking, Happy, Alert,
  EyeGlowPulse, HUDRotate, WaveformPulse
"""
import math
import random
import sys

import bpy
import bmesh
from mathutils import Vector

import os

FACE_IN = sys.argv[1]
GLB_OUT = sys.argv[2]
PORTRAIT = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] not in ("", "-") else None
RENDER_DIR = sys.argv[4] if len(sys.argv) > 4 else None

# Landmark fractions of the portrait image (v measured from the BOTTOM).
EYE_V = float(os.getenv("NOVA_PORTRAIT_EYE_V", "0.565"))
MOUTH_V = float(os.getenv("NOVA_PORTRAIT_MOUTH_V", "0.395"))
EYE_SPAN = float(os.getenv("NOVA_PORTRAIT_EYE_SPAN", "0.205"))

random.seed(7)
FPS = 30

# Palette
PURPLE = (0.545, 0.361, 0.965, 1.0)
VIOLET = (0.655, 0.545, 0.980, 1.0)
BLUE = (0.322, 0.659, 1.000, 1.0)
GOLD = (0.961, 0.772, 0.259, 1.0)
INK = (0.055, 0.030, 0.160, 1.0)
SKIN = (0.42, 0.38, 0.92, 1.0)

HEAD_CENTER = Vector((0.0, 0.02, 1.42))
HEAD_HEIGHT = 0.95


# ── Helpers ──────────────────────────────────────────────────────────────────

def clear_scene():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete()
    for block in (bpy.data.meshes, bpy.data.materials, bpy.data.curves, bpy.data.images, bpy.data.actions):
        for item in list(block):
            if item.users == 0:
                block.remove(item)


def make_material(name, base=PURPLE, emission=None, strength=1.0, alpha=1.0, rough=0.35, metal=0.05):
    mat = bpy.data.materials.new(name)
    try:
        mat.use_nodes = True
    except Exception:
        pass
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    bsdf.inputs["Base Color"].default_value = base
    bsdf.inputs["Roughness"].default_value = rough
    bsdf.inputs["Metallic"].default_value = metal
    bsdf.inputs["Alpha"].default_value = alpha
    if emission is not None:
        bsdf.inputs["Emission Color"].default_value = emission
        bsdf.inputs["Emission Strength"].default_value = strength
    if alpha < 1.0:
        for attr, value in (("blend_method", "BLEND"), ("surface_render_method", "BLENDED")):
            try:
                setattr(mat, attr, value)
            except Exception:
                pass
    return mat


def new_empty(name, parent=None, location=(0, 0, 0)):
    empty = bpy.data.objects.new(name, None)
    empty.location = location
    bpy.context.collection.objects.link(empty)
    if parent is not None:
        empty.parent = parent
    return empty


def link_obj(obj, parent=None):
    if obj.name not in bpy.context.collection.objects:
        bpy.context.collection.objects.link(obj)
    if parent is not None:
        obj.parent = parent
    return obj


def uv_sphere(name, radius=1.0, segments=32, rings=24, location=(0, 0, 0), scale=(1, 1, 1), mat=None, parent=None):
    bpy.ops.mesh.primitive_uv_sphere_add(segments=segments, ring_count=rings, radius=radius, location=location)
    obj = bpy.context.active_object
    obj.name = name
    obj.scale = scale
    if mat:
        obj.data.materials.append(mat)
    if parent:
        obj.parent = parent
    bpy.ops.object.shade_smooth()
    return obj


def torus(name, major=1.0, minor=0.01, location=(0, 0, 0), rotation=(0, 0, 0), mat=None, parent=None, major_seg=96, minor_seg=8):
    bpy.ops.mesh.primitive_torus_add(major_radius=major, minor_radius=minor, location=location, rotation=rotation,
                                     major_segments=major_seg, minor_segments=minor_seg)
    obj = bpy.context.active_object
    obj.name = name
    if mat:
        obj.data.materials.append(mat)
    if parent:
        obj.parent = parent
    bpy.ops.object.shade_smooth()
    return obj


def build_lofted_bust(name, ring_defs, mat, parent, segments=48):
    """Loft one continuous, watertight bust mesh from horizontal elliptical
    cross-section rings (chest -> shoulder -> neck), bridging each pair of
    adjacent rings instead of overlapping separate primitives — this is what
    gives an organic, seamless taper instead of three disjointed shapes.

    ring_defs: list of (z, radius_x, radius_y, center_y) from bottom to top.
    """
    bm = bmesh.new()
    loops = []
    for (z, rx, ry, cy) in ring_defs:
        verts = [
            bm.verts.new((math.cos(a) * rx, math.sin(a) * ry + cy, z))
            for a in (2 * math.pi * i / segments for i in range(segments))
        ]
        edges = [bm.edges.new((verts[i], verts[(i + 1) % segments])) for i in range(segments)]
        loops.append(edges)

    for i in range(len(loops) - 1):
        bmesh.ops.bridge_loops(bm, edges=loops[i] + loops[i + 1])

    # Cap both ends so the loft is a single watertight solid.
    bmesh.ops.edgeloop_fill(bm, edges=loops[0])
    bmesh.ops.edgeloop_fill(bm, edges=loops[-1])
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)

    mesh = bpy.data.meshes.new(f"{name}_Mesh")
    bm.to_mesh(mesh)
    bm.free()

    obj = bpy.data.objects.new(name, mesh)
    link_obj(obj, parent=parent)
    if mat:
        obj.data.materials.append(mat)

    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.shade_smooth()

    # Light subdivision smooths the ring facets into an organic transition
    # between chest, shoulder, and neck.
    mod = obj.modifiers.new("BustSubsurf", type="SUBSURF")
    mod.levels = 1
    mod.render_levels = 2
    bpy.ops.object.modifier_apply(modifier=mod.name)

    return obj


def stash_clip(datablock_owner, action, clip_name, start=1):
    """Push an action into an NLA track named after the clip (exported as a named animation)."""
    ad = datablock_owner.animation_data or datablock_owner.animation_data_create()
    ad.action = None
    track = ad.nla_tracks.new()
    track.name = clip_name
    strip = track.strips.new(action.name, int(start), action)
    strip.name = clip_name
    return strip


def animate_object(obj, clip_name, keys, data_paths=("location",)):
    """keys: list of (frame, {path: value}). Creates one action, stashes as clip."""
    ad = obj.animation_data or obj.animation_data_create()
    action = bpy.data.actions.new(f"{clip_name}_{obj.name}")
    ad.action = action
    try:
        if ad.action_slot is None and action.slots:
            ad.action_slot = action.slots[0]
    except Exception:
        pass
    for frame, values in keys:
        for path, value in values.items():
            setattr(obj, path, value)
            obj.keyframe_insert(data_path=path, frame=frame)
    stash_clip(obj, action, clip_name)


def animate_shape_keys(mesh_obj, clip_name, keys):
    """keys: list of (frame, {shape_key_name: value}). All referenced keys reset to 0 at frame 1."""
    shape_keys = mesh_obj.data.shape_keys
    ad = shape_keys.animation_data or shape_keys.animation_data_create()
    action = bpy.data.actions.new(f"{clip_name}_keys")
    ad.action = action
    try:
        if ad.action_slot is None and action.slots:
            ad.action_slot = action.slots[0]
    except Exception:
        pass
    names = set()
    for _, values in keys:
        names.update(values.keys())
    for frame, values in keys:
        for name in names:
            kb = shape_keys.key_blocks.get(name)
            if kb is None:
                continue
            kb.value = values.get(name, kb.value if frame > 1 else 0.0)
            kb.keyframe_insert(data_path="value", frame=frame)
    for name in names:
        kb = shape_keys.key_blocks.get(name)
        if kb is not None:
            kb.value = 0.0
    stash_clip(shape_keys, action, clip_name)


# ── Scene assembly ───────────────────────────────────────────────────────────

clear_scene()
scene = bpy.context.scene
scene.render.fps = FPS

nova_root = new_empty("Nova_Root")
head_root = new_empty("Head_Root", parent=nova_root)

# 1) Import the realistic face (52 ARKit shape keys)
before = set(bpy.data.objects)
bpy.ops.import_scene.gltf(filepath=FACE_IN)
imported = [o for o in bpy.data.objects if o not in before]

face_meshes = [o for o in imported if o.type == "MESH"]

# Prefer explicit names from the MPFB2 head pipeline (build_nova_head.py
# exports "Head"/"Eye_L"/"Eye_R"/"Teeth"); fall back to the old facecap
# heuristic (shape-keyed mesh = face; two smallest meshes = eyes; rest = teeth)
# for backward compatibility with facecap_clean.glb.
_by_name = {o.name: o for o in face_meshes}
_named_head = _by_name.get("Head") or _by_name.get("Face")
if _named_head is not None:
    face_obj = _named_head
else:
    face_obj = next(o for o in face_meshes if o.data.shape_keys and len(o.data.shape_keys.key_blocks) > 10)

# Purge the source model's own animations (mocap takes) so only our clips export.
for o in imported:
    if o.animation_data:
        o.animation_data_clear()
    if o.type == "MESH" and o.data.shape_keys and o.data.shape_keys.animation_data:
        o.data.shape_keys.animation_data_clear()
for action in list(bpy.data.actions):
    bpy.data.actions.remove(action)

# Normalize: measure combined mesh bbox, scale head to HEAD_HEIGHT, center at HEAD_CENTER.
mins = Vector((1e9, 1e9, 1e9))
maxs = Vector((-1e9, -1e9, -1e9))
for o in face_meshes:
    for corner in o.bound_box:
        wc = o.matrix_world @ Vector(corner)
        mins = Vector(map(min, mins, wc))
        maxs = Vector(map(max, maxs, wc))
size = maxs - mins
center = (maxs + mins) / 2
scale_factor = HEAD_HEIGHT / max(size.z, 1e-6)

# Find import roots (objects with no parent among imported) and wrap them.
import_roots = [o for o in imported if o.parent is None or o.parent not in imported]
for o in import_roots:
    o.parent = head_root

head_root.scale = (scale_factor,) * 3
head_root.location = HEAD_CENTER - center * scale_factor

# Classify meshes: prefer explicit names from the MPFB2 head pipeline;
# otherwise fall back to the old facecap heuristic (geometry-based — source
# nodes are unnamed there: the two smallest meshes are eyes (L/R by X),
# teeth is the rest).
_named_eye_l = _by_name.get("Eye_L")
_named_eye_r = _by_name.get("Eye_R")
_named_teeth = _by_name.get("Teeth")
face_obj.name = "Nova_Face"
others = [o for o in face_meshes if o is not face_obj]


def _center_x(o):
    lo = o.matrix_world @ Vector(o.bound_box[0])
    hi = o.matrix_world @ Vector(o.bound_box[6])
    return ((lo + hi) / 2).x


def _size(o):
    lo = Vector(o.bound_box[0])
    hi = Vector(o.bound_box[6])
    return (hi - lo).length


if _named_eye_l is not None and _named_eye_r is not None:
    _named_eye_l.name = "Nova_Eye_L"
    _named_eye_r.name = "Nova_Eye_R"
    if _named_teeth is not None:
        _named_teeth.name = "Nova_Teeth"
else:
    others.sort(key=_size)
    eye_candidates = others[:2] if len(others) >= 2 else []
    eye_candidates.sort(key=_center_x)
    if len(eye_candidates) == 2:
        eye_candidates[0].name = "Nova_Eye_L"
        eye_candidates[1].name = "Nova_Eye_R"
    for o in others[2:]:
        o.name = "Nova_Teeth"


# Hologram materials on the face set
mat_skin = make_material("NovaHolo_Skin", base=(0.52, 0.46, 0.99, 1.0), emission=VIOLET, strength=0.28, alpha=0.85, rough=0.42)
mat_eye = make_material("NovaHolo_Eye", base=(0.75, 0.7, 1.0, 1.0), emission=PURPLE, strength=2.1, alpha=1.0, rough=0.2)
mat_teeth = make_material("NovaHolo_Teeth", base=INK, emission=VIOLET, strength=0.06, alpha=1.0)

for o in face_meshes:
    o.data.materials.clear()
    if "Eye" in o.name:
        o.data.materials.append(mat_eye)
    elif "Teeth" in o.name:
        o.data.materials.append(mat_teeth)
    else:
        o.data.materials.append(mat_skin)

# Scanline/circuit emissive texture on the face (UV: smart-project if missing)
img = bpy.data.images.new("NovaCircuitTex", width=512, height=512, alpha=False)
px = [0.0] * (512 * 512 * 4)


def put(x, y, r, g, b, a=1.0):
    if 0 <= x < 512 and 0 <= y < 512:
        i = (y * 512 + x) * 4
        px[i], px[i + 1], px[i + 2], px[i + 3] = max(px[i], r), max(px[i + 1], g), max(px[i + 2], b), 1.0


# Violet base glow (the emissive texture REPLACES flat emission, so the skin's
# luminosity must live in the texture itself) + scan lines + circuit traces.
for y in range(512):
    for x in range(512):
        i = (y * 512 + x) * 4
        px[i], px[i + 1], px[i + 2], px[i + 3] = 0.135, 0.105, 0.30, 1.0
for y in range(0, 512, 6):  # scan lines
    for x in range(512):
        put(x, y, 0.22, 0.17, 0.44)
for _ in range(46):  # circuit traces (manhattan walks)
    x, y = random.randrange(512), random.randrange(512)
    for _seg in range(random.randint(3, 7)):
        length = random.randint(14, 60)
        dx, dy = random.choice([(1, 0), (-1, 0), (0, 1), (0, -1)])
        for _s in range(length):
            x, y = (x + dx) % 512, (y + dy) % 512
            put(x, y, 0.16, 0.10, 0.42)
        put(x, y, 0.5, 0.4, 1.0)
        put(x + 1, y, 0.5, 0.4, 1.0)
        put(x, y + 1, 0.5, 0.4, 1.0)
img.pixels = px
img.pack()

if not face_obj.data.uv_layers:
    bpy.context.view_layer.objects.active = face_obj
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.uv.smart_project(angle_limit=math.radians(66))
    bpy.ops.object.mode_set(mode="OBJECT")

nodes = mat_skin.node_tree.nodes
links = mat_skin.node_tree.links
tex_node = nodes.new("ShaderNodeTexImage")
tex_node.image = img
bsdf = nodes.get("Principled BSDF")
links.new(tex_node.outputs["Color"], bsdf.inputs["Emission Color"])
bsdf.inputs["Emission Strength"].default_value = 1.5

# Eye glow pulse discs
eye_objs = {}
for name in ("Nova_Eye_L", "Nova_Eye_R"):
    o = bpy.data.objects.get(name)
    if o:
        eye_objs[name] = o

mat_glow = make_material("NovaHolo_EyeGlow", base=PURPLE, emission=PURPLE, strength=3.4, alpha=0.5)
glow_objs = []
for suffix, eye_name in (("L", "Nova_Eye_L"), ("R", "Nova_Eye_R")):
    eye = eye_objs.get(eye_name)
    if eye is None:
        continue
    wc = eye.matrix_world @ (Vector(eye.bound_box[0]) + Vector(eye.bound_box[6])) / 2
    glow = uv_sphere(f"EyeGlow_{suffix}", radius=0.045, segments=16, rings=12,
                     location=(wc.x, wc.y - 0.035, wc.z), scale=(1, 0.4, 1), mat=mat_glow, parent=head_root)
    glow.matrix_parent_inverse = head_root.matrix_world.inverted()
    glow_objs.append(glow)

# 1b) Portrait projection onto the front of the face (optional)
def _world_center(obj):
    lo = obj.matrix_world @ Vector(obj.bound_box[0])
    hi = obj.matrix_world @ Vector(obj.bound_box[6])
    return (lo + hi) / 2


if PORTRAIT:
    if PORTRAIT == "@test":
        # Calibration image: skin-toned field with crosshairs at the expected
        # eye (green) and mouth (red) landmarks.
        psize = 1024
        pimg = bpy.data.images.new("NovaPortrait", width=psize, height=psize, alpha=False)
        buf = [0.0] * (psize * psize * 4)
        for y in range(psize):
            shade = 0.5 + 0.3 * (y / psize)
            for x in range(psize):
                i = (y * psize + x) * 4
                buf[i], buf[i + 1], buf[i + 2], buf[i + 3] = shade * 0.85, shade * 0.72, shade * 0.98, 1.0

        def pput(x, y, r, g, b):
            if 0 <= x < psize and 0 <= y < psize:
                i = (y * psize + x) * 4
                buf[i], buf[i + 1], buf[i + 2], buf[i + 3] = r, g, b, 1.0

        def cross(cx, cy, r, g, b):
            X, Y = int(cx * psize), int(cy * psize)
            for dd in range(-40, 41):
                for w in range(-3, 4):
                    pput(X + dd, Y + w, r, g, b)
                    pput(X + w, Y + dd, r, g, b)

        cross(0.5 - EYE_SPAN / 2, EYE_V, 0.05, 1.0, 0.1)
        cross(0.5 + EYE_SPAN / 2, EYE_V, 0.05, 1.0, 0.1)
        cross(0.5, MOUTH_V, 1.0, 0.08, 0.08)
        pimg.pixels = buf
        pimg.pack()
    else:
        pimg = bpy.data.images.load(PORTRAIT)
        # Blue-violet hologram grade, keeping facial detail via luminance.
        # NOVA_PORTRAIT_GRADE 0..1 blends original->graded (use low values for
        # portraits that are already hologram-toned).
        grade = float(os.getenv("NOVA_PORTRAIT_GRADE", "1.0"))
        if grade > 0.001:
            import numpy as np

            arr = np.array(pimg.pixels[:], dtype=np.float32).reshape(-1, 4)
            rgb = arr[:, :3]
            lum = rgb @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
            tint = np.array([0.60, 0.54, 1.06], dtype=np.float32)
            graded = np.clip((rgb * 0.42 + np.outer(lum, tint) * 0.66) * 1.1, 0.0, 1.0)
            arr[:, :3] = rgb * (1.0 - grade) + graded * grade
            pimg.pixels = arr.reshape(-1).tolist()
        pimg.pack()

    # Geometry landmarks (world space) BEFORE any eye-mesh removal.
    bpy.context.view_layer.update()
    lm_eye_l = bpy.data.objects.get("Nova_Eye_L")
    lm_eye_r = bpy.data.objects.get("Nova_Eye_R")
    lm_teeth = bpy.data.objects.get("Nova_Teeth")
    el, er, mo = _world_center(lm_eye_l), _world_center(lm_eye_r), _world_center(lm_teeth)

    # Affine world→UV so eyes/mouth land on the image landmarks.
    ua = EYE_SPAN / max(er.x - el.x, 1e-6)
    ub = 0.5 - ua * (er.x + el.x) / 2
    eye_z = (el.z + er.z) / 2
    vc = (EYE_V - MOUTH_V) / max(eye_z - mo.z, 1e-6)
    vd = EYE_V - vc * eye_z

    mesh = face_obj.data
    uv_proj = mesh.uv_layers.new(name="PortraitProj")
    mw = face_obj.matrix_world
    for loop in mesh.loops:
        co = mw @ mesh.vertices[loop.vertex_index].co
        uv_proj.data[loop.index].uv = (ua * co.x + ub, vc * co.z + vd)

    # Portrait material (slot 1) on front-facing polygons; violet skin elsewhere.
    mat_portrait = bpy.data.materials.new("NovaHolo_Portrait")
    try:
        mat_portrait.use_nodes = True
    except Exception:
        pass
    pb = mat_portrait.node_tree.nodes.get("Principled BSDF")
    ptex = mat_portrait.node_tree.nodes.new("ShaderNodeTexImage")
    ptex.image = pimg
    ptex.extension = "EXTEND"  # clamp edges; REPEAT would tile hair across the scalp
    uv_node = mat_portrait.node_tree.nodes.new("ShaderNodeUVMap")
    uv_node.uv_map = "PortraitProj"
    plinks = mat_portrait.node_tree.links
    plinks.new(uv_node.outputs["UV"], ptex.inputs["Vector"])
    plinks.new(ptex.outputs["Color"], pb.inputs["Base Color"])
    plinks.new(ptex.outputs["Color"], pb.inputs["Emission Color"])
    pb.inputs["Emission Strength"].default_value = float(os.getenv("NOVA_PORTRAIT_EMISSION", "0.4"))
    pb.inputs["Roughness"].default_value = 0.5
    pb.inputs["Alpha"].default_value = 0.97
    for attr, value in (("blend_method", "BLEND"), ("surface_render_method", "BLENDED")):
        try:
            setattr(mat_portrait, attr, value)
        except Exception:
            pass

    face_obj.data.materials.append(mat_portrait)
    portrait_slot = len(face_obj.data.materials) - 1
    nmat = mw.to_3x3().inverted().transposed()
    for poly in mesh.polygons:
        world_normal = (nmat @ poly.normal).normalized()
        if world_normal.y < -0.28:
            poly.material_index = portrait_slot

    # Keep the 3D eyeballs (they fill the eyelid openings and blink correctly)
    # but turn them into glowing purple orbs — the reference's luminous eyes.
    mat_orb = make_material("NovaHolo_EyeOrb", base=(0.09, 0.035, 0.28, 1.0), emission=(0.42, 0.22, 0.95, 1.0), strength=0.5, alpha=1.0, rough=0.3)
    for lm in (lm_eye_l, lm_eye_r):
        lm.data.materials.clear()
        lm.data.materials.append(mat_orb)

    # Glow discs are redundant over glowing orbs — remove them in portrait mode
    # and let the EyeGlowPulse clip breathe the orbs themselves (subtly).
    for glow in list(glow_objs):
        bpy.data.objects.remove(glow, do_unlink=True)
    glow_objs.clear()
    EYE_PULSE_TARGETS = [(lm_eye_l, 1.05), (lm_eye_r, 1.05)]
else:
    EYE_PULSE_TARGETS = None


# 2) Holographic bob hair (curve strands → mesh)
hair_root = new_empty("Hair_Root", parent=head_root)
hair_root.matrix_parent_inverse = head_root.matrix_world.inverted()
mat_hair = make_material("NovaHolo_Hair", base=INK, emission=(0.42, 0.30, 0.95, 1.0), strength=0.5, alpha=0.92, rough=0.5)
mat_hair_glow = make_material("NovaHolo_HairGlow", base=INK, emission=BLUE, strength=1.7, alpha=0.9)

hc = HEAD_CENTER
r_scalp = 0.40
strands = []
for i in range(110):
    # az 0 = +Y (back of head); keep the face (-Y side) fully open, and keep
    # strands originating from the crown/back rather than the temples so
    # none of them read as disconnected from the silhouette.
    az = math.radians(random.uniform(-65, 65))
    ph = math.radians(random.uniform(12, 62))
    dx = math.sin(ph) * math.sin(az)
    dy = math.sin(ph) * math.cos(az)
    dz = math.cos(ph)
    start = hc + Vector((dx, dy, dz)) * (r_scalp * random.uniform(0.96, 1.02))
    out = Vector((dx, dy, 0)).normalized() if abs(dx) + abs(dy) > 1e-4 else Vector((0, 1, 0))

    back = 0.04  # bias falling strands behind the face plane
    length_jitter = random.uniform(-0.04, 0.04)
    p0 = start
    p1 = start + Vector((dx, dy, dz)) * 0.05 + Vector((0, 0, 0.015))
    # p2/p3/p4 fall relative to THIS strand's own start height, not a fixed
    # head-center Z — otherwise strands starting at different heights all
    # get dragged toward the same absolute depth, reading as disconnected
    # background strands.
    p2 = Vector((start.x + out.x * 0.06, start.y + out.y * 0.06 + back, start.z - 0.12 + random.uniform(-0.04, 0.04)))
    p3 = Vector((p2.x * 0.96, p2.y * 0.96 + 0.02 + back, start.z - 0.46 + length_jitter))
    p4 = Vector((p3.x * 0.90, p3.y * 0.90 + 0.03 + back, start.z - 0.68 + length_jitter))

    curve = bpy.data.curves.new(f"HairStrand_{i:03d}", type="CURVE")
    curve.dimensions = "3D"
    curve.bevel_depth = random.uniform(0.006, 0.013)
    curve.bevel_resolution = 2
    curve.resolution_u = 8
    spline = curve.splines.new("NURBS")
    pts = [p0, p1, p2, p3, p4]
    spline.points.add(len(pts) - 1)
    for j, p in enumerate(pts):
        spline.points[j].co = (p.x, p.y, p.z, 1.0)
        spline.points[j].radius = max(0.15, 1.0 - j * 0.22)
    spline.use_endpoint_u = True
    obj = bpy.data.objects.new(curve.name, curve)
    link_obj(obj)
    strands.append(obj)

bpy.ops.object.select_all(action="DESELECT")
for s in strands:
    s.select_set(True)
bpy.context.view_layer.objects.active = strands[0]
bpy.ops.object.convert(target="MESH")
bpy.ops.object.join()
hair = bpy.context.active_object
hair.name = "Nova_Hair"
hair.data.materials.append(mat_hair)
hair.data.materials.append(mat_hair_glow)
# every ~6th face-island glows: assign by polygon ranges (approximation: alternate blocks)
for pi, poly in enumerate(hair.data.polygons):
    poly.material_index = 1 if (pi // 97) % 7 == 0 else 0
hair.parent = hair_root

# 3) Glass bust: one continuous lofted neck/shoulders/chest form, plus collar/ring/core accents
bust_root = new_empty("Bust_Root", parent=nova_root)
mat_glass = make_material("NovaHolo_Glass", base=(0.35, 0.30, 0.85, 1.0), emission=PURPLE, strength=0.35, alpha=0.30, rough=0.15)
mat_gold = make_material("NovaHolo_Gold", base=GOLD, emission=GOLD, strength=1.5, alpha=0.95, metal=0.6, rough=0.25)

# Horizontal cross-section rings from chest to neck-top (z, radius_x, radius_y,
# center_y), skinned into one lofted, watertight mesh instead of three
# disjointed primitives — no hard seams, no abrupt bottleneck taper.
BUST_RINGS = [
    (0.08, 0.46, 0.29, 0.02),   # chest base
    (0.30, 0.44, 0.27, 0.02),   # mid-chest
    (0.58, 0.60, 0.27, 0.02),   # shoulder (widest, flattened ellipse)
    (0.72, 0.36, 0.22, 0.015),  # shoulder -> neck transition (gradual, not a bottleneck)
    (0.83, 0.21, 0.17, 0.01),   # neck base
    (0.97, 0.16, 0.14, 0.01),   # neck shaft
    (1.07, 0.135, 0.125, 0.01),  # neck top, tucks under the head
]
bust_body = build_lofted_bust("Bust_Body", BUST_RINGS, mat_glass, bust_root)

torus("Bust_Collar", major=0.205, minor=0.008, location=(0, 0.01, 0.795), mat=mat_gold, parent=bust_root)
torus("Bust_Ring", major=0.44, minor=0.006, location=(0, 0.02, 0.50), mat=make_material("NovaHolo_RingViolet", base=VIOLET, emission=VIOLET, strength=1.3, alpha=0.85), parent=bust_root)

bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=1, radius=0.045, location=(0, -0.16, 0.52))
core = bpy.context.active_object
core.name = "Bust_Core"
core.data.materials.append(mat_gold)
core.parent = bust_root

# 4) HUD halo behind the head
hud_root = new_empty("HUD_Root", parent=nova_root, location=(0, 0.40, 1.40))
mat_ring_p = make_material("NovaHolo_HUDPurple", base=PURPLE, emission=PURPLE, strength=1.4, alpha=0.8)
mat_ring_b = make_material("NovaHolo_HUDBlue", base=BLUE, emission=BLUE, strength=1.2, alpha=0.7)
mat_ring_g = make_material("NovaHolo_HUDGold", base=GOLD, emission=GOLD, strength=1.5, alpha=0.85)

spin_a = new_empty("HUD_Spin_A", parent=hud_root)
spin_b = new_empty("HUD_Spin_B", parent=hud_root)
spin_c = new_empty("HUD_Spin_C", parent=hud_root)

torus("HUD_Ring_Outer", major=1.22, minor=0.007, rotation=(math.pi / 2, 0, 0), mat=mat_ring_p, parent=spin_a)
torus("HUD_Ring_Mid", major=1.05, minor=0.005, rotation=(math.pi / 2, 0, 0), mat=mat_ring_b, parent=spin_b)
torus("HUD_Ring_Inner", major=0.88, minor=0.006, rotation=(math.pi / 2, 0, 0), mat=mat_ring_p, parent=spin_c)

# Radial ticks (72) on the outer ring plane (XZ)
tick_objs = []
for i in range(72):
    a = i / 72 * 2 * math.pi
    r = 1.30
    bpy.ops.mesh.primitive_cube_add(size=1, location=(math.cos(a) * r, 0, math.sin(a) * r))
    tick = bpy.context.active_object
    tick.scale = (0.004, 0.004, 0.03 if i % 6 else 0.055)
    tick.rotation_euler = (0, -a, 0)
    tick_objs.append(tick)
bpy.ops.object.select_all(action="DESELECT")
for tobj in tick_objs:
    tobj.select_set(True)
bpy.context.view_layer.objects.active = tick_objs[0]
bpy.ops.object.join()
ticks = bpy.context.active_object
ticks.name = "HUD_Ticks"
ticks.data.materials.append(mat_ring_b)
ticks.parent = spin_a

# Scan arcs (partial circles, gold + blue)
for arc_name, radius, a0, a1, mat, parent in (
    ("HUD_Arc_Gold", 1.14, 20, 130, mat_ring_g, spin_b),
    ("HUD_Arc_Blue", 0.97, 200, 275, mat_ring_b, spin_c),
):
    curve = bpy.data.curves.new(arc_name, type="CURVE")
    curve.dimensions = "3D"
    curve.bevel_depth = 0.009
    curve.bevel_resolution = 2
    spline = curve.splines.new("POLY")
    steps = 28
    spline.points.add(steps)
    for j in range(steps + 1):
        a = math.radians(a0 + (a1 - a0) * j / steps)
        spline.points[j].co = (math.cos(a) * radius, 0, math.sin(a) * radius, 1)
    obj = bpy.data.objects.new(arc_name, curve)
    link_obj(obj, parent=parent)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.ops.object.convert(target="MESH")
    obj.data.materials.append(mat)

# Energy particles scattered on an annulus
particle_objs = []
for i in range(70):
    a = random.uniform(0, 2 * math.pi)
    r = random.uniform(0.72, 1.42)
    y = random.uniform(-0.12, 0.12)
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=1, radius=random.uniform(0.004, 0.011),
                                          location=(math.cos(a) * r, y, math.sin(a) * r))
    particle_objs.append(bpy.context.active_object)
bpy.ops.object.select_all(action="DESELECT")
for pobj in particle_objs:
    pobj.select_set(True)
bpy.context.view_layer.objects.active = particle_objs[0]
bpy.ops.object.join()
particles = bpy.context.active_object
particles.name = "HUD_Particles"
particles.data.materials.append(make_material("NovaHolo_Particle", base=VIOLET, emission=VIOLET, strength=2.2, alpha=0.75))
particles.parent = spin_b

# 5) Waveform bars (48) across the chest front
wave_root = new_empty("Waveform_Root", parent=nova_root, location=(0, -0.58, 0.46))
mat_wave = make_material("NovaHolo_Wave", base=PURPLE, emission=PURPLE, strength=2.0, alpha=0.9)
bars = []
for i in range(48):
    x = -0.59 + i * 0.025
    bpy.ops.mesh.primitive_cube_add(size=1, location=(x, 0, 0))
    bar = bpy.context.active_object
    bar.name = f"Waveform_Bar_{i:02d}"
    bar.scale = (0.008, 0.008, 0.028)
    bar.data.materials.append(mat_wave)
    bar.parent = wave_root
    bars.append(bar)

# 6) Camera (front view) + soft area lights
cam_data = bpy.data.cameras.new("Nova_Camera")
cam = bpy.data.objects.new("Nova_Camera", cam_data)
cam.location = (0, -4.1, 1.30)
cam.rotation_euler = (math.pi / 2, 0, 0)
link_obj(cam)
scene.camera = cam

for lname, loc, energy, color, size in (
    ("Light_Key", (0.8, -2.6, 2.3), 120, (0.85, 0.8, 1.0), 2.0),
    ("Light_Rim", (-1.2, 1.8, 2.0), 90, (0.55, 0.45, 1.0), 2.5),
):
    ldata = bpy.data.lights.new(lname, type="AREA")
    ldata.energy = energy
    ldata.color = color
    ldata.size = size
    lobj = bpy.data.objects.new(lname, ldata)
    lobj.location = loc
    direction = Vector((0, 0, 1.2)) - Vector(loc)
    lobj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    link_obj(lobj)

# ── Animation clips ──────────────────────────────────────────────────────────

def deg(x):
    return math.radians(x)

# IdleHover: root bob + gentle sway (8s loop)
animate_object(nova_root, "IdleHover", [
    (1, {"location": (0, 0, 0.0), "rotation_euler": (0, 0, 0)}),
    (80, {"location": (0, 0, 0.03), "rotation_euler": (0, deg(0.6), deg(1.2))}),
    (160, {"location": (0, 0, -0.015), "rotation_euler": (0, deg(-0.6), deg(-1.2))}),
    (240, {"location": (0, 0, 0.0), "rotation_euler": (0, 0, 0)}),
])

# HUDRotate: three rings, opposing slow spins (30s)
animate_object(spin_a, "HUDRotate", [(1, {"rotation_euler": (0, 0, 0)}), (900, {"rotation_euler": (0, deg(360), 0)})])
animate_object(spin_b, "HUDRotate", [(1, {"rotation_euler": (0, 0, 0)}), (900, {"rotation_euler": (0, deg(-360), 0)})])
animate_object(spin_c, "HUDRotate", [(1, {"rotation_euler": (0, 0, 0)}), (900, {"rotation_euler": (0, deg(540), 0)})])

# EyeGlowPulse (3s) — glow discs (default) or eye orbs (portrait mode)
pulse_targets = EYE_PULSE_TARGETS if EYE_PULSE_TARGETS else [(g, 1.3) for g in glow_objs]
for pulse_obj, factor in pulse_targets:
    base = tuple(pulse_obj.scale)
    animate_object(pulse_obj, "EyeGlowPulse", [
        (1, {"scale": base}),
        (45, {"scale": (base[0] * factor, base[1] * factor, base[2] * factor)}),
        (90, {"scale": base}),
    ])

# WaveformPulse: bars ripple (3.2s)
for i, bar in enumerate(bars):
    ad = bar.animation_data or bar.animation_data_create()
    action = bpy.data.actions.new(f"WaveformPulse_{bar.name}")
    ad.action = action
    try:
        if ad.action_slot is None and action.slots:
            ad.action_slot = action.slots[0]
    except Exception:
        pass
    for frame in range(1, 97, 8):
        phase = (frame / 96) * 2 * math.pi * 2 + i * 0.45
        height = 0.028 + max(0.0, math.sin(phase)) * 0.075 + random.uniform(0, 0.012)
        bar.scale = (0.008, 0.008, height)
        bar.keyframe_insert(data_path="scale", frame=frame)
    stash_clip(bar, action, "WaveformPulse")

# Shape-key clips on the face
animate_shape_keys(face_obj, "Blink", [
    (1, {"eyeBlink_L": 0.0, "eyeBlink_R": 0.0}),
    (5, {"eyeBlink_L": 1.0, "eyeBlink_R": 1.0}),
    (11, {"eyeBlink_L": 0.0, "eyeBlink_R": 0.0}),
])

speak_keys = [(1, {"jawOpen": 0.0, "mouthFunnel": 0.0, "mouthPucker": 0.0})]
for frame in range(7, 73, 6):
    speak_keys.append((frame, {
        "jawOpen": random.uniform(0.15, 0.6),
        "mouthFunnel": random.uniform(0.0, 0.35),
        "mouthPucker": random.uniform(0.0, 0.2),
    }))
speak_keys.append((72, {"jawOpen": 0.0, "mouthFunnel": 0.0, "mouthPucker": 0.0}))
animate_shape_keys(face_obj, "Speaking", speak_keys)

animate_shape_keys(face_obj, "Listening", [
    (1, {"browInnerUp": 0.0, "eyeWide_L": 0.0, "eyeWide_R": 0.0}),
    (14, {"browInnerUp": 0.30, "eyeWide_L": 0.35, "eyeWide_R": 0.35}),
    (90, {"browInnerUp": 0.30, "eyeWide_L": 0.35, "eyeWide_R": 0.35}),
])

animate_shape_keys(face_obj, "Thinking", [
    (1, {"browDown_L": 0.0, "browDown_R": 0.0, "eyeLookUp_L": 0.0, "eyeLookUp_R": 0.0, "mouthPress_L": 0.0, "mouthPress_R": 0.0}),
    (18, {"browDown_L": 0.42, "browDown_R": 0.34, "eyeLookUp_L": 0.5, "eyeLookUp_R": 0.5, "mouthPress_L": 0.3, "mouthPress_R": 0.3}),
    (120, {"browDown_L": 0.42, "browDown_R": 0.34, "eyeLookUp_L": 0.5, "eyeLookUp_R": 0.5, "mouthPress_L": 0.3, "mouthPress_R": 0.3}),
])

animate_shape_keys(face_obj, "Happy", [
    (1, {"mouthSmile_L": 0.0, "mouthSmile_R": 0.0, "cheekSquint_L": 0.0, "cheekSquint_R": 0.0}),
    (16, {"mouthSmile_L": 0.72, "mouthSmile_R": 0.68, "cheekSquint_L": 0.35, "cheekSquint_R": 0.35}),
    (45, {"mouthSmile_L": 0.72, "mouthSmile_R": 0.68, "cheekSquint_L": 0.35, "cheekSquint_R": 0.35}),
])

animate_shape_keys(face_obj, "Alert", [
    (1, {"eyeWide_L": 0.0, "eyeWide_R": 0.0, "browInnerUp": 0.0, "jawOpen": 0.0}),
    (8, {"eyeWide_L": 0.85, "eyeWide_R": 0.85, "browInnerUp": 0.6, "jawOpen": 0.1}),
    (30, {"eyeWide_L": 0.85, "eyeWide_R": 0.85, "browInnerUp": 0.6, "jawOpen": 0.05}),
])

# Head motion component of Listening (subtle tilt) — same NLA track name merges into the clip.
animate_object(head_root, "Listening", [
    (1, {"rotation_euler": (0, 0, 0)}),
    (14, {"rotation_euler": (deg(1.5), 0, deg(3.5))}),
    (90, {"rotation_euler": (deg(1.5), 0, deg(3.5))}),
])
animate_object(head_root, "Thinking", [
    (1, {"rotation_euler": (0, 0, 0)}),
    (18, {"rotation_euler": (deg(-2.5), 0, deg(-3))}),
    (120, {"rotation_euler": (deg(-2.5), 0, deg(-3))}),
])

# ── Export ───────────────────────────────────────────────────────────────────
bpy.ops.object.select_all(action="SELECT")

export_kwargs = dict(
    filepath=GLB_OUT,
    export_format="GLB",
    export_animation_mode="NLA_TRACKS",
    export_cameras=True,
    export_lights=True,
    export_morph=True,
    export_yup=True,
    export_apply=False,
)
try:
    bpy.ops.export_scene.gltf(**export_kwargs)
except TypeError:
    export_kwargs.pop("export_lights", None)
    bpy.ops.export_scene.gltf(**export_kwargs)

print("EXPORTED:", GLB_OUT)

# ── Verification renders (Cycles CPU, low samples) ──────────────────────────
if RENDER_DIR:
    os.makedirs(RENDER_DIR, exist_ok=True)

    # NLA tracks would override manually-posed shape keys during render.
    for datablock in list(bpy.data.objects) + [face_obj.data.shape_keys]:
        ad = getattr(datablock, "animation_data", None)
        if ad:
            for tr in ad.nla_tracks:
                tr.mute = True

    scene.render.engine = "CYCLES"
    scene.cycles.samples = 24
    try:
        scene.cycles.use_denoising = False
    except Exception:
        pass
    scene.render.resolution_x = 512
    scene.render.resolution_y = 640
    world = bpy.data.worlds.new("NovaWorld")
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg:
        bg.inputs[0].default_value = (0.004, 0.003, 0.012, 1.0)
        bg.inputs[1].default_value = 1.0
    scene.world = world

    def snap(name):
        scene.render.filepath = os.path.join(RENDER_DIR, name)
        bpy.ops.render.render(write_still=True)
        print("RENDERED:", name)

    kb = face_obj.data.shape_keys.key_blocks

    snap("front.png")

    if "jawOpen" in kb:
        kb["jawOpen"].value = 0.65
    snap("jaw_open.png")
    if "jawOpen" in kb:
        kb["jawOpen"].value = 0.0

    for n in ("eyeBlink_L", "eyeBlink_R"):
        if n in kb:
            kb[n].value = 1.0
    snap("blink.png")
    for n in ("eyeBlink_L", "eyeBlink_R"):
        if n in kb:
            kb[n].value = 0.0
