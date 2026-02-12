const { contextBridge } = require("electron");

// Intentionally minimal: the React app already resolves API base via window.__NOVA_API_BASE.
// This bridge is here if you later want to expose safe, whitelisted desktop-only APIs.
contextBridge.exposeInMainWorld("novaDesktop", {
  platform: process.platform,
});
