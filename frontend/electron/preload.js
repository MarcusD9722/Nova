const { contextBridge, ipcRenderer } = require("electron");

// Intentionally minimal: the React app already resolves API base via window.__NOVA_API_BASE.
// This bridge is here if you later want to expose safe, whitelisted desktop-only APIs.
contextBridge.exposeInMainWorld("novaDesktop", {
  platform: process.platform,
  openModelFolder: async () => {
    try {
      return await ipcRenderer.invoke("nova:openModelFolder");
    } catch {
      return { ok: false };
    }
  },
  captureScreen: async () => {
    try {
      return await ipcRenderer.invoke("nova:captureScreen");
    } catch (e) {
      return { ok: false, error: String(e?.message || e) };
    }
  },
  desktopGestureStatus: async () => {
    try {
      return await ipcRenderer.invoke("nova:desktopGestureStatus");
    } catch {
      return { ok: false, supported: false, platform: process.platform };
    }
  },
  moveSystemCursor: async (point) => {
    try {
      return await ipcRenderer.invoke("nova:desktopMoveCursor", point || {});
    } catch {
      return { ok: false };
    }
  },
  clickSystemCursor: async (point) => {
    try {
      return await ipcRenderer.invoke("nova:desktopClick", point || {});
    } catch {
      return { ok: false };
    }
  },
  mouseDownSystemCursor: async (point) => {
    try {
      return await ipcRenderer.invoke("nova:desktopMouseDown", point || {});
    } catch {
      return { ok: false };
    }
  },
  mouseUpSystemCursor: async (point) => {
    try {
      return await ipcRenderer.invoke("nova:desktopMouseUp", point || {});
    } catch {
      return { ok: false };
    }
  },
  windowMinimize: async () => {
    try {
      return await ipcRenderer.invoke("nova:windowMinimize");
    } catch {
      return { ok: false };
    }
  },
  windowToggleMaximize: async () => {
    try {
      return await ipcRenderer.invoke("nova:windowToggleMaximize");
    } catch {
      return { ok: false };
    }
  },
  windowClose: async () => {
    try {
      return await ipcRenderer.invoke("nova:windowClose");
    } catch {
      return { ok: false };
    }
  },
});

// Let the React app discover the backend base URL in production.
// It already checks window.__NOVA_API_BASE.
try {
  const base = process.env.NOVA_API_BASE ? String(process.env.NOVA_API_BASE) : "";
  if (base) contextBridge.exposeInMainWorld("__NOVA_API_BASE", base.replace(/\/$/, ""));
} catch {}

// API auth token (Phase 0.3): flows from the NOVA_API_TOKEN env var into the
// renderer so the fetch wrapper in src/lib/apiToken.js can attach it. Only
// ever sent to Nova's own API base, never third-party hosts.
try {
  const token = process.env.NOVA_API_TOKEN ? String(process.env.NOVA_API_TOKEN) : "";
  if (token) contextBridge.exposeInMainWorld("__NOVA_API_TOKEN", token);
} catch {}
