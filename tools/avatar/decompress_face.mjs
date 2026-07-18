// Decompress facecap.glb for Blender: decode meshopt, dequantize, drop KTX2 textures.
import { NodeIO } from "@gltf-transform/core";
import { ALL_EXTENSIONS } from "@gltf-transform/extensions";
import { dequantize, prune } from "@gltf-transform/functions";
import { MeshoptDecoder } from "meshoptimizer";

const [,, inPath, outPath] = process.argv;

const io = new NodeIO()
  .registerExtensions(ALL_EXTENSIONS)
  .registerDependencies({ "meshopt.decoder": MeshoptDecoder });

const doc = await io.read(inPath);

// Remove all textures (we re-materialize in Blender with hologram shaders).
for (const tex of doc.getRoot().listTextures()) tex.dispose();

await doc.transform(dequantize(), prune());

// Ensure no compression/quantization extensions linger in the output.
for (const ext of doc.getRoot().listExtensionsUsed()) {
  const name = ext.extensionName;
  if (["EXT_meshopt_compression", "KHR_mesh_quantization", "KHR_texture_basisu", "KHR_texture_transform"].includes(name)) {
    ext.dispose();
  }
}

await io.write(outPath, doc);

// Report
const doc2 = await io.read(outPath);
const root = doc2.getRoot();
console.log("meshes:", root.listMeshes().length);
const prims = root.listMeshes().flatMap((m) => m.listPrimitives());
console.log("morph targets on first prim:", prims[0]?.listTargets().length ?? 0);
console.log("extensions:", root.listExtensionsUsed().map((e) => e.extensionName));
console.log("OK");
