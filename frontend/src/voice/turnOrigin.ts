// src/voice/turnOrigin.ts
//
// How a chat request says where it came from (V3 P5.1e).
//
// Voice identity is decided by the BACKEND from an opaque one-use handle. The
// client's only jobs are to (a) say the turn arrived by voice and (b) forward
// the handle if `/stt` returned one. Everything else — who is speaking, what
// their role is, how confident the match was — is resolved server-side from the
// redeemed handle and is never asserted here.
//
// The single most important rule in this file:
//
//     a voice turn with NO handle is still a VOICE turn.
//
// Dropping `input_source` when the handle is missing would silently promote an
// unverified utterance to typed-owner semantics, which is the exact failure the
// whole P5.1 boundary exists to prevent. So `inputSource` and `voiceTurnId` are
// independent: the first is transport, the second is evidence.

export type TurnOrigin = {
  /** "voice" for microphone-originated turns. Typed chat passes nothing. */
  inputSource?: "voice";
  /** Opaque, single-use, backend-minted. Never generated or persisted here. */
  voiceTurnId?: string | null;
};

/** Fields a client may NEVER send. Identity is backend-derived, always. */
export const FORBIDDEN_ORIGIN_FIELDS = [
  "speaker", "profile_id", "display_name", "role", "owner",
  "similarity", "threshold", "speaker_status", "is_owner",
] as const;

/**
 * The origin fields to merge into a chat request body.
 *
 * Returns `{}` for typed turns so their payload stays byte-for-byte what it was
 * before P5.1e — a typed message must not gain voice keys, even null ones.
 */
export function originFields(origin?: TurnOrigin | null): Record<string, unknown> {
  if (!origin || origin.inputSource !== "voice") return {};
  const out: Record<string, unknown> = { input_source: "voice" };
  const handle = origin.voiceTurnId;
  if (typeof handle === "string" && handle.trim()) out.voice_turn_id = handle.trim();
  return out;
}

/** Body for POST /chat/stream. */
export function buildStreamBody(args: {
  text: string;
  attachments?: unknown[];
  conversationId?: string | null;
  location?: unknown;
  origin?: TurnOrigin | null;
}): Record<string, unknown> {
  return {
    msg: args.text || null,
    attachments: args.attachments ?? [],
    hint: "",
    speak: true,
    ...(args.conversationId ? { conversation_id: args.conversationId } : {}),
    ...(args.location ? { current_location: args.location } : {}),
    ...originFields(args.origin),
  };
}

/**
 * Body for the non-streaming POST /chat fallback.
 *
 * Carries the SAME origin as the stream attempt. If the stream already redeemed
 * the handle, the backend's replay protection resolves this retry as unverified
 * — which is correct and safe. Omitting the origin to "help" the retry succeed
 * would instead upgrade it to typed owner, so the handle is passed unchanged
 * and the backend decides.
 */
export function buildFallbackBody(args: {
  text: string;
  attachments?: unknown[];
  conversationId?: string | null;
  location?: unknown;
  origin?: TurnOrigin | null;
}): Record<string, unknown> {
  return {
    message: args.text || null,
    attachments: args.attachments ?? [],
    ...(args.conversationId ? { conversation_id: args.conversationId } : {}),
    ...(args.location ? { current_location: args.location } : {}),
    ...originFields(args.origin),
  };
}

/** Origin for a clean microphone command, given whatever `/stt` returned. */
export function voiceOrigin(voiceTurnId?: string | null): TurnOrigin {
  return { inputSource: "voice", voiceTurnId: voiceTurnId ?? null };
}

/**
 * Origin for text salvaged from a barge-in.
 *
 * Deliberately handle-less. The barge-in capture is MIXED audio — the human
 * plus Nova's own output leaking through the speakers — so it is never speaker-
 * classified, and reusing the handle from the command Nova was answering would
 * attribute one person's interruption to whoever spoke before them.
 */
export function unverifiedVoiceOrigin(): TurnOrigin {
  return { inputSource: "voice", voiceTurnId: null };
}
