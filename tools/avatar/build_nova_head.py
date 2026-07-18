"""Generate + beautify a true-3D parametric female head via MPFB2 (MakeHuman-in-Blender).

MUST be run with the bpy==4.2 interpreter (scratchpad/bpy42env), NOT the main
bpyenv (bpy 5) used by build_nova_avatar.py — MPFB2 targets Blender 4.2 exactly.

Usage:
    bpy42env\\Scripts\\python.exe build_nova_head.py <mpfb_src_dir> <glb_out> [renders_dir]

<mpfb_src_dir> = the ".../mpfb2-2.0.16/src" directory (contains the "mpfb" package).

Produces a GLB with FOUR top-level, unparented mesh objects — "Head", "Eye_L",
"Eye_R", "Teeth" — matching the naming build_nova_avatar.py's classification
step looks for first (falling back to its old facecap heuristic otherwise).
"Head" carries shape keys: Basis, jawOpen, eyeBlink_L, eyeBlink_R,
mouthSmile_L, mouthSmile_R. No rig, no armature, no clothes/hair (the
hologram pipeline supplies its own hair/eyes styling downstream).

Pipeline:
  1. HumanService.create_human() -> fully-female, young-adult macro details.
  2. Load beautification targets (bigger eyes, softer/narrower jaw, fuller
     lips, refined nose) and TargetService.bake_targets() to permanently
     sculpt them into the mesh (this is safe: baking never changes vertex
     count/order, so it can happen before further target loads).
  3. Load ARKit expression targets as NAMED, un-baked shape keys (jawOpen,
     eyeBlink_L/R, mouthSmile_L/R) — this MUST happen while the mesh still
     has its original, full topology, because target files are vertex-index
     deltas that only line up with that topology.
  4. Capture world-space landmarks (eye sockets, teeth, lips, scalp, chest)
     from vertex groups while the full helper geometry still exists.
  5. Delete helper geometry (hair/skirt/tights/genital/eye/teeth "helper"
     verts — MakeHuman renders these as separate fitted proxies which we
     don't have) and everything below mid-chest, via an EDIT-MODE vertex
     delete (not a modifier-apply) so all shape keys stay in lockstep.
  6. Build simple placeholder Eye_L/Eye_R/Teeth meshes at the captured
     landmarks (the hologram pipeline turns eyes into glowing orbs anyway,
     so realism here doesn't matter — position/scale does).
  7. Optional low-sample Cycles verification renders.
  8. Export GLB (no animation, no rig).
"""
import sys
import os
import shutil
import random
import importlib

import bpy
import bmesh
from mathutils import Vector

MPFB_SRC = os.path.abspath(sys.argv[1])
GLB_OUT = os.path.abspath(sys.argv[2])
RENDER_DIR = os.path.abspath(sys.argv[3]) if len(sys.argv) > 3 else None

assert os.path.isdir(os.path.join(MPFB_SRC, "mpfb")), "mpfb package not found under " + MPFB_SRC

MODULE = "bl_ext.user_default.mpfb"


def _install_extension():
    """Copy the mpfb package into Blender's user_default extensions repo
    (idempotent) so it can be enabled as a proper Blender 4.2 extension —
    MPFB2's LocationService requires bpy.utils.extension_path_user(), which
    only works for modules under the bl_ext.<repo>.<pkg> namespace."""
    repo_dir = None
    for r in bpy.context.preferences.extensions.repos:
        if r.module == "user_default":
            repo_dir = r.directory
            break
    assert repo_dir, "user_default extensions repo not found"
    dst = os.path.join(repo_dir, "mpfb")
    src = os.path.join(MPFB_SRC, "mpfb")
    if not os.path.isdir(dst):
        os.makedirs(repo_dir, exist_ok=True)
        shutil.copytree(src, dst)


_install_extension()
result = bpy.ops.preferences.addon_enable(module=MODULE)
assert MODULE in bpy.context.preferences.addons, "MPFB2 failed to enable: %s" % (result,)


def dynamic_import(suffix, key):
    for name in list(sys.modules):
        if name.endswith(suffix):
            mod = importlib.import_module(name)
            if hasattr(mod, key):
                return getattr(mod, key)
    raise ValueError("No module found with name ending in " + suffix)


HumanService = dynamic_import("mpfb.services.humanservice", "HumanService")
HumanObjectProperties = dynamic_import("mpfb.entities.objectproperties", "HumanObjectProperties")
TargetService = dynamic_import("mpfb.services.targetservice", "TargetService")
LocationService = dynamic_import("mpfb.services.locationservice", "LocationService")

TARGETS_ROOT = LocationService.get_mpfb_data("targets")


def target_path(*parts):
    p = os.path.join(TARGETS_ROOT, *parts)
    assert os.path.exists(p), "missing target file: " + p
    return p


def clear_scene():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete()
    for block in (bpy.data.meshes, bpy.data.materials, bpy.data.actions, bpy.data.images):
        for item in list(block):
            if item.users == 0:
                block.remove(item)


clear_scene()
random.seed(11)

# ── 1) Base human: fully female, young adult, average build ────────────────
human = HumanService.create_human()
human.name = "Human"

HumanObjectProperties.set_value("gender", 0.0, entity_reference=human)  # 0.0=female, 1.0=male
HumanObjectProperties.set_value("age", 0.45, entity_reference=human)    # 0.5=="young"; slightly younger
TargetService.reapply_macro_details(human)

# ── 2) Beautification targets — softened toward the reference (elegant,
# narrow/oval face, big eyes, fuller lips, refined nose) — then bake them
# permanently into the mesh (safe: doesn't change vertex count/order).
BEAUTY_TARGETS = [
    (("eyes", "l-eye-scale-incr.target.gz"), 0.55),
    (("eyes", "r-eye-scale-incr.target.gz"), 0.55),
    (("head", "head-oval.target.gz"), 0.45),
    (("chin", "chin-width-decr.target.gz"), 0.30),
    (("chin", "chin-prominent-decr.target.gz"), 0.20),
    (("mouth", "mouth-lowerlip-volume-incr.target.gz"), 0.45),
    (("mouth", "mouth-upperlip-volume-incr.target.gz"), 0.35),
    (("nose", "nose-scale-horiz-decr.target.gz"), 0.35),
    (("nose", "nose-width2-decr.target.gz"), 0.30),
    (("nose", "nose-volume-decr.target.gz"), 0.20),
]
for parts, weight in BEAUTY_TARGETS:
    TargetService.load_target(human, target_path(*parts), weight=weight)

TargetService.bake_targets(human)

# ── 3) ARKit expression shape keys — MUST load onto the still-full-topology
# mesh (target files are vertex-index deltas). Kept as separate keys (not
# baked) so the frontend can drive them 0..1 at runtime.
EXPR_DIR = os.path.join(TARGETS_ROOT, "expression", "units", "caucasian")


def expr_path(name):
    p = os.path.join(EXPR_DIR, name)
    assert os.path.exists(p), "missing expression target: " + p
    return p


TargetService.load_target(human, expr_path("mouth-open.target.gz"), weight=0.0, name="jawOpen")
TargetService.load_target(human, expr_path("eye-left-closure.target.gz"), weight=0.0, name="eyeBlink_L")
TargetService.load_target(human, expr_path("eye-right-closure.target.gz"), weight=0.0, name="eyeBlink_R")
TargetService.load_target(human, expr_path("mouth-corner-puller.target.gz"), weight=0.0, name="mouthSmile_L")
TargetService.load_target(human, expr_path("mouth-corner-puller.target.gz"), weight=0.0, name="mouthSmile_R")

# ── 4) Capture world-space landmarks from vertex groups BEFORE removing any
# helper geometry (eye sockets, teeth, lips, scalp, chest/nipple line).
bpy.context.view_layer.update()
mesh = human.data


def group_index(name):
    vg = human.vertex_groups.get(name)
    return vg.index if vg else None


def group_centroid_and_size(*names):
    gidxs = [group_index(n) for n in names if group_index(n) is not None]
    if not gidxs:
        return None, None
    pts = []
    for v in mesh.vertices:
        for g in v.groups:
            if g.group in gidxs and g.weight > 0.05:
                pts.append(human.matrix_world @ v.co)
                break
    if not pts:
        return None, None
    xs, ys, zs = [p.x for p in pts], [p.y for p in pts], [p.z for p in pts]
    center = Vector(((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, (min(zs) + max(zs)) / 2))
    size = Vector((max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)))
    return center, size


eye_l_c, eye_l_s = group_centroid_and_size("helper-l-eye")
eye_r_c, eye_r_s = group_centroid_and_size("helper-r-eye")
teeth_c, teeth_s = group_centroid_and_size("helper-upper-teeth", "helper-lower-teeth")
lips_c, lips_s = group_centroid_and_size("lips")
scalp_c, scalp_s = group_centroid_and_size("scalp")
chest_c, chest_s = group_centroid_and_size("nippleTip", "nipple")

assert eye_l_c and eye_r_c, "could not locate eye socket vertex groups"

# ── 5) Delete helper geometry + everything below mid-chest via EDIT MODE
# vertex delete (NOT modifier-apply — Blender refuses/breaks modifier-apply
# on a mesh with multiple shape keys, but edit-mode vertex deletion trims
# every shape key's data in lockstep correctly).
body_gi = group_index("body")
cut_z = (chest_c.z - 0.03) if chest_c else -1e9

bpy.context.view_layer.objects.active = human
bpy.ops.object.mode_set(mode="EDIT")
bm = bmesh.from_edit_mesh(mesh)
bm.verts.ensure_lookup_table()
dvert_layer = bm.verts.layers.deform.active
for v in bm.verts:
    in_body = False
    if dvert_layer is not None and body_gi is not None:
        dvert = v[dvert_layer]
        in_body = body_gi in dvert and dvert[body_gi] > 0.5
    world_z = (human.matrix_world @ v.co).z
    v.select = (not in_body) or (world_z < cut_z)
bmesh.update_edit_mesh(mesh)
bpy.ops.mesh.delete(type="VERT")

# Cap the open chest/neck rim left by the cut (otherwise the translucent
# hologram bust shows a jagged open edge through the neckline).
bpy.ops.mesh.select_all(action="DESELECT")
bpy.ops.mesh.fill_holes(sides=0)

bpy.ops.object.mode_set(mode="OBJECT")

# The "Hide helpers" mask modifier is now a no-op (only "body" verts remain
# and helper groups are gone) — drop it so export is clean.
for m in list(human.modifiers):
    human.modifiers.remove(m)

human.name = "Head"

# ── 6) Placeholder Eye_L/Eye_R/Teeth meshes at the captured landmarks. The
# hologram pipeline replaces these with glowing purple orbs downstream, so
# only position/scale matter, not realism.


def make_eye(name, center, size):
    radius = min(max((size.x + size.y + size.z) / 3 * 0.5, 0.008), 0.02) if size else 0.014
    bpy.ops.mesh.primitive_uv_sphere_add(segments=20, ring_count=14, radius=radius, location=center)
    obj = bpy.context.active_object
    obj.name = name
    bpy.ops.object.shade_smooth()
    return obj


eye_obj_l = make_eye("Eye_L", eye_l_c, eye_l_s)
eye_obj_r = make_eye("Eye_R", eye_r_c, eye_r_s)

teeth_center = teeth_c if teeth_c else (lips_c if lips_c else Vector((0, 0, 0)))
teeth_size = teeth_s if teeth_s else Vector((0.06, 0.02, 0.02))
bpy.ops.mesh.primitive_cube_add(size=1, location=teeth_center)
teeth_obj = bpy.context.active_object
teeth_obj.name = "Teeth"
teeth_obj.scale = (max(teeth_size.x * 0.85, 0.04), max(teeth_size.y * 0.7, 0.010), max(teeth_size.z * 0.55, 0.012))
bpy.ops.object.shade_smooth()

# ── 7) Verification renders (Cycles, low samples — same pattern as
# build_nova_avatar.py) ------------------------------------------------------
if RENDER_DIR:
    os.makedirs(RENDER_DIR, exist_ok=True)
    scene = bpy.context.scene

    mat_skin = bpy.data.materials.new("PreviewSkin")
    mat_skin.use_nodes = True
    bsdf = mat_skin.node_tree.nodes.get("Principled BSDF")
    bsdf.inputs["Base Color"].default_value = (0.86, 0.74, 0.68, 1.0)
    bsdf.inputs["Roughness"].default_value = 0.55
    human.data.materials.append(mat_skin)

    import math

    cam_data = bpy.data.cameras.new("PreviewCam")
    cam = bpy.data.objects.new("PreviewCam", cam_data)
    head_top = scalp_c.z if scalp_c else 1.6
    cam.location = (0, -0.75, head_top - 0.12)
    cam.rotation_euler = (math.radians(90), 0, 0)
    bpy.context.collection.objects.link(cam)
    scene.camera = cam

    for lname, loc, energy in (("Key", (0.6, -1.4, head_top + 0.3), 400), ("Rim", (-0.8, 0.8, head_top), 250)):
        ldata = bpy.data.lights.new(lname, type="AREA")
        ldata.energy = energy
        ldata.size = 1.2
        lobj = bpy.data.objects.new(lname, ldata)
        lobj.location = loc
        direction = Vector((0, 0, head_top)) - Vector(loc)
        lobj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
        bpy.context.collection.objects.link(lobj)

    scene.render.engine = "CYCLES"
    scene.cycles.samples = 24
    try:
        scene.cycles.use_denoising = False
    except Exception:
        pass
    scene.render.resolution_x = 512
    scene.render.resolution_y = 640
    world = bpy.data.worlds.new("PreviewWorld")
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg:
        bg.inputs[0].default_value = (0.02, 0.02, 0.03, 1.0)
    scene.world = world

    def snap(name):
        scene.render.filepath = os.path.join(RENDER_DIR, name)
        bpy.ops.render.render(write_still=True)
        print("RENDERED:", name)

    kb = human.data.shape_keys.key_blocks
    snap("head_front.png")
    if "jawOpen" in kb:
        kb["jawOpen"].value = 0.7
    snap("head_jaw_open.png")
    if "jawOpen" in kb:
        kb["jawOpen"].value = 0.0
    for n in ("eyeBlink_L", "eyeBlink_R"):
        if n in kb:
            kb[n].value = 1.0
    snap("head_blink.png")
    for n in ("eyeBlink_L", "eyeBlink_R"):
        if n in kb:
            kb[n].value = 0.0

    human.data.materials.clear()

# ── 8) Export GLB (meshes + shape keys only — no rig, no animation) ────────
bpy.ops.object.select_all(action="SELECT")
bpy.ops.export_scene.gltf(
    filepath=GLB_OUT,
    export_format="GLB",
    export_morph=True,
    export_yup=True,
    export_apply=False,
)
print("EXPORTED:", GLB_OUT)
