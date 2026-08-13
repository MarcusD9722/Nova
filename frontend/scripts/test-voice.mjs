/**
 * Runs the voice unit tests outside a browser.
 *
 * bargeIn.ts imports recorder.ts, which touches AudioContext / MediaStream and
 * cannot load under node. Only the PURE decision functions are under test, so
 * recorder is replaced with a stub — the timing wrapper genuinely needs a
 * browser and is covered by the live acceptance run instead.
 *
 *   npm run test:voice
 */
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "..");
const out = path.join(root, ".voicetest");

fs.rmSync(out, { recursive: true, force: true });
fs.mkdirSync(out, { recursive: true });

// esbuild's JS API rather than its CLI: node 24 on Windows refuses to
// spawnSync a .cmd shim (EINVAL), and shelling out just to avoid an import
// would trade a real bug for a platform quirk.
const { build } = await import("esbuild");
await build({
  entryPoints: [path.join(root, "src/voice/bargeIn.ts")],
  format: "esm",
  outfile: path.join(out, "bargeIn.js"),
  logLevel: "error",
});

fs.writeFileSync(path.join(out, "recorder.js"),
  "export function getTtsOutputRms() { return 0; }\n" +
  "export function isTtsPlaying() { return false; }\n");

// node's ESM resolver requires the extension that esbuild leaves off.
const bundle = path.join(out, "bargeIn.js");
fs.writeFileSync(bundle,
  fs.readFileSync(bundle, "utf8").replace('from "./recorder"', 'from "./recorder.js"'));

fs.copyFileSync(path.join(root, "src/voice/bargeIn.test.mjs"),
  path.join(out, "test.mjs"));

execFileSync(process.execPath, [path.join(out, "test.mjs")], { stdio: "inherit" });
