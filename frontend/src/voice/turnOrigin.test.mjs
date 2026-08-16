/**
 * V3 P5.1e — the ACTUAL voice transport contract.
 *
 * These assert real request payloads, not the presence of a string in App.jsx.
 * The backend reads `speaker` as a multipart FORM FIELD, so "the source
 * contains speaker=true" would prove nothing about what the browser sends —
 * the FormData is inspected directly, and so are the two chat JSON bodies.
 *
 * Run:  npm run test:voice   (from frontend/)
 */

import {
  buildFallbackBody, buildStreamBody, originFields,
  unverifiedVoiceOrigin, voiceOrigin, FORBIDDEN_ORIGIN_FIELDS,
} from "./turnOrigin.js";
import { transcribeBlobDetailed, voiceTurnIdOf } from "./recorder.js";

let failed = 0;
function check(cond, label) {
  const status = cond ? "OK  " : "FAIL";
  if (!cond) failed += 1;
  console.log(`  ${status} ${label}`);
}

// ── a fetch stub that records exactly what production would have sent ───────
function stubFetch(jsonBody) {
  const calls = [];
  const fn = async (url, init) => {
    calls.push({ url, init });
    return {
      ok: true,
      async json() { return jsonBody; },
      async text() { return JSON.stringify(jsonBody); },
    };
  };
  fn.calls = calls;
  return fn;
}

const BLOB = new Blob([new Uint8Array([1, 2, 3])], { type: "audio/webm" });

console.log("\nSTT multipart: speaker is a FORM FIELD, and absent by default");
{
  const f = stubFetch({ text: "hello", empty: false });
  await transcribeBlobDetailed(BLOB, { url: "/stt", fetchImpl: f });
  check(f.calls.length === 1, "exactly one upload");
  const fd = f.calls[0].init.body;
  check(fd instanceof FormData, "the body is multipart FormData");
  check(fd.get("file") !== null, "carrying the audio file");
  check(fd.get("speaker") === null,
    `wake / barge-in captures send NO speaker field (got ${fd.get("speaker")})`);
}
{
  const f = stubFetch({ text: "hello", speaker: { voice_turn_id: "vt-abc", status: "known" } });
  const r = await transcribeBlobDetailed(BLOB, { url: "/stt", speaker: true, fetchImpl: f });
  const fd = f.calls[0].init.body;
  check(fd.get("speaker") === "true",
    `a clean command sends speaker="true" as a form field (got ${fd.get("speaker")})`);
  check(f.calls[0].url === "/stt", "posted to /stt, not /stt?speaker=true");
  check(f.calls.length === 1, "ONE upload — identity does not cost a second STT");
  check(voiceTurnIdOf(r) === "vt-abc", "the handle is read off the same response");
  check(r.speaker?.status === "known", "diagnostics are exposed to the client");
}
{
  const f = stubFetch({ text: "", empty: true, speaker: { status: "unavailable", voice_turn_id: null } });
  const r = await transcribeBlobDetailed(BLOB, { url: "/stt", speaker: true, fetchImpl: f });
  check(voiceTurnIdOf(r) === null, "a null handle reads back as null, not as a string");
  check(r.empty === true, "and an empty transcript is reported");
}

console.log("\ntyped chat stays byte-for-byte legacy");
{
  const body = buildStreamBody({ text: "hi", conversationId: "c1" });
  check(!("input_source" in body), "no input_source key at all");
  check(!("voice_turn_id" in body), "no voice_turn_id key at all");
  check(body.msg === "hi" && body.speak === true && body.hint === "",
    "and the existing fields are unchanged");
  const fb = buildFallbackBody({ text: "hi", conversationId: "c1" });
  check(!("input_source" in fb) && !("voice_turn_id" in fb),
    "the fallback body is equally untouched");
  check(fb.message === "hi", "using the fallback's own field name");
}

console.log("\nclean voice command: both transports carry the same origin");
{
  const origin = voiceOrigin("vt-abc");
  const stream = buildStreamBody({ text: "what's the weather", origin, conversationId: "c1" });
  const fb = buildFallbackBody({ text: "what's the weather", origin, conversationId: "c1" });
  check(stream.input_source === "voice", "stream says voice");
  check(stream.voice_turn_id === "vt-abc", "stream carries the handle");
  check(fb.input_source === "voice", "fallback says voice");
  check(fb.voice_turn_id === "vt-abc", "fallback carries the SAME handle");
}

console.log("\nthe rule that matters most: no handle is still VOICE");
{
  for (const [label, o] of [
    ["null handle", voiceOrigin(null)],
    ["undefined handle", voiceOrigin(undefined)],
    ["empty-string handle", voiceOrigin("   ")],
    ["barge-in salvage", unverifiedVoiceOrigin()],
  ]) {
    const body = buildStreamBody({ text: "x", origin: o });
    check(body.input_source === "voice", `${label}: still input_source=voice`);
    check(!("voice_turn_id" in body), `${label}: and no handle key`);
    const fb = buildFallbackBody({ text: "x", origin: o });
    check(fb.input_source === "voice", `${label}: fallback too`);
  }
  // The failure this prevents, stated as an assertion.
  const noHandle = buildStreamBody({ text: "x", origin: voiceOrigin(null) });
  check(Object.keys(noHandle).includes("input_source"),
    "a missing handle can never be mistaken for typed input");
}

console.log("\nthe client never asserts an identity");
{
  const bodies = [
    buildStreamBody({ text: "x", origin: voiceOrigin("vt-abc") }),
    buildFallbackBody({ text: "x", origin: voiceOrigin("vt-abc") }),
    originFields(voiceOrigin("vt-abc")),
  ];
  for (const b of bodies) {
    const leaked = FORBIDDEN_ORIGIN_FIELDS.filter((k) => k in b);
    check(leaked.length === 0, `no client-asserted identity fields (${leaked.join(",") || "none"})`);
  }
  check(Object.keys(originFields(voiceOrigin("vt-abc"))).sort().join(",")
        === "input_source,voice_turn_id",
    "the ONLY origin keys are input_source and voice_turn_id");
}

console.log("\nreplayed handle: the retry keeps the origin, backend decides");
{
  // The frontend deliberately does NOT drop or regenerate the handle on retry.
  // If the stream already redeemed it, the backend resolves the retry as
  // unverified — safe. Omitting it would make the retry typed-owner instead.
  const origin = voiceOrigin("vt-once");
  const first = buildStreamBody({ text: "x", origin });
  const retry = buildFallbackBody({ text: "x", origin });
  check(first.voice_turn_id === retry.voice_turn_id,
    "the same handle is presented, not a fresh one");
  check(retry.input_source === "voice",
    "and the retry is still voice, so it can only fall back to UNVERIFIED");
}

console.log("\nsession commands are independent");
{
  const a = voiceOrigin("vt-marcus-1");
  const b = voiceOrigin("vt-alice-2");
  check(buildStreamBody({ text: "1", origin: a }).voice_turn_id !==
        buildStreamBody({ text: "2", origin: b }).voice_turn_id,
    "each command carries its own handle — a session is not one identity");
}

console.log("\nRESULT:", failed ? `${failed} FAILURES` : "ALL PASS");
process.exit(failed ? 1 : 0);
