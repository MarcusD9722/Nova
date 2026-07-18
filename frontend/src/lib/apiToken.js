// API auth token plumbing (Phase 0.3 of docs/ROADMAP.md).
//
// Token sources, in order: Electron preload injection (window.__NOVA_API_TOKEN,
// from the NOVA_API_TOKEN env var) → localStorage "nova_api_token" (set once in
// devtools or a future Settings field). Empty string = no auth, which matches
// the backend: it only enforces when NOVA_API_TOKEN is set server-side.

export function novaApiToken() {
  try {
    if (window.__NOVA_API_TOKEN) return String(window.__NOVA_API_TOKEN);
  } catch {}
  try {
    return window.localStorage.getItem("nova_api_token") || "";
  } catch {}
  return "";
}

export function novaApiBase() {
  try {
    if (window.__NOVA_API_BASE) return String(window.__NOVA_API_BASE).replace(/\/$/, "");
  } catch {}
  return "http://localhost:8008";
}

// Install a single global fetch wrapper that attaches the Bearer token to
// requests targeting Nova's API — and ONLY Nova's API. The configured API
// base is the trust anchor: relative URLs (Vite dev proxy → same backend)
// and absolute URLs on the API base origin get the header; every other host
// (Google Maps tiles, HuggingFace, anything third-party) never sees it.
export function installAuthFetch() {
  const token = novaApiToken();
  if (!token) return;
  const base = novaApiBase();
  const rawFetch = window.fetch.bind(window);
  window.fetch = (input, init = {}) => {
    try {
      const url = typeof input === "string" ? input : String(input?.url || "");
      const isApi = url.startsWith("/") || url === base || url.startsWith(base + "/");
      if (isApi) {
        const headers = new Headers(
          init.headers || (typeof input === "object" ? input.headers : undefined) || {}
        );
        if (!headers.has("Authorization")) headers.set("Authorization", `Bearer ${token}`);
        init = { ...init, headers };
      }
    } catch {}
    return rawFetch(input, init);
  };
}
