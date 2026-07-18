const { app, BrowserWindow, shell, ipcMain, screen, desktopCapturer } = require("electron");
const { spawn } = require("child_process");
const fs = require("fs");
const http = require("http");
const net = require("net");
const path = require("path");
const { isSupported: isDesktopInputSupported, moveCursor, leftDown, leftUp, leftClick, dispose: disposeDesktopInput } = require("./windows-input");

// Allow programmatic audio playback (TTS) without requiring a user gesture.
// This is particularly important because TTS playback happens after async fetch.
try {
  app.commandLine.appendSwitch("autoplay-policy", "no-user-gesture-required");
} catch {}

// Nova does not benefit much from Chromium's on-disk HTTP cache, and stale cache
// state has been causing blockfile corruption errors on some Windows installs.
try {
  app.commandLine.appendSwitch("disable-http-cache");
} catch {}

function ensureDirSync(dirPath) {
  try {
    fs.mkdirSync(dirPath, { recursive: true });
  } catch {}
}

function configureSessionDataPath() {
  try {
    const userData = app.getPath("userData");
    const sessionData = path.join(userData, "session");
    ensureDirSync(userData);
    ensureDirSync(sessionData);
    app.setPath("sessionData", sessionData);
  } catch {}
}

function resetChromiumCaches() {
  try {
    const sessionData = app.getPath("sessionData");
    for (const name of ["Cache", "Code Cache", "GPUCache", "DawnCache"]) {
      try {
        fs.rmSync(path.join(sessionData, name), { recursive: true, force: true });
      } catch {}
    }
  } catch {}
}

configureSessionDataPath();

function isDev() {
  return !app.isPackaged;
}

let backendProc = null;

function resourcesPath() {
  // In dev, process.resourcesPath points at Electron's resources.
  // In production, it's the app's resources directory.
  return process.resourcesPath;
}

function backendExePath() {
  // We bundle the PyInstaller --onedir output into resources/backend/**
  // so the executable is at: resources/backend/nova-backend.exe
  return path.join(resourcesPath(), "backend", "nova-backend.exe");
}

function canBind(port) {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.unref();
    server.on("error", () => resolve(false));
    server.listen({ host: "127.0.0.1", port }, () => {
      server.close(() => resolve(true));
    });
  });
}

async function findFreePort(preferred = 8008) {
  const start = Number(preferred) || 8008;
  for (let p = start; p < start + 50; p++) {
    // eslint-disable-next-line no-await-in-loop
    if (await canBind(p)) return p;
  }
  return start;
}

function probeBackendHealth(port) {
  return new Promise((resolve) => {
    const req = http.get(
      {
        host: "127.0.0.1",
        port,
        path: "/health",
        timeout: 1000,
      },
      (res) => {
        res.resume();
        resolve(res.statusCode >= 200 && res.statusCode < 500);
      }
    );
    req.on("error", () => resolve(false));
    req.on("timeout", () => {
      req.destroy();
      resolve(false);
    });
  });
}

async function waitForBackendReady(port, timeoutMs = 12000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    // eslint-disable-next-line no-await-in-loop
    if (await probeBackendHealth(port)) return true;
    // eslint-disable-next-line no-await-in-loop
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  return false;
}

function startBackendIfPackaged(port) {
  if (isDev()) return null;

  const exe = backendExePath();

  const userData = app.getPath("userData");
  // Create user-writable dirs eagerly (avoid backend failing on first run).
  try {
    fs.mkdirSync(userData, { recursive: true });
    fs.mkdirSync(path.join(userData, "projects"), { recursive: true });
    fs.mkdirSync(path.join(userData, "memory_data"), { recursive: true });
    fs.mkdirSync(path.join(userData, "model"), { recursive: true });
  } catch {}

  const env = {
    ...process.env,
    // Bind backend locally.
    NOVA_HOST: "127.0.0.1",
    NOVA_PORT: String(port),
    // Make the backend write to a user-writable sandbox.
    NOVA_REPO_ROOT: userData,
    NOVA_PROJECTS_DIR: path.join(userData, "projects"),
    NOVA_MEMORY_DIR: path.join(userData, "memory_data"),
    // Model is NOT bundled: users drop any *.gguf into %APPDATA%\\Nova\\model
    // (or set NOVA_MODEL_PATH / NOVA_MODEL_DIR themselves).
    NOVA_MODEL_DIR: path.join(userData, "model"),
    NOVA_VOICE_DIR: path.join(resourcesPath(), "voices"),
    NOVA_DEFAULT_VOICE: "nova_198b4ad1.wav",
    NOVA_TTS_DEVICE: "cuda",
    NOVA_TTS_WARMUP_TEXT: "Hello there. I am ready to help.",
    // Match dev defaults (autonomy on by default).
    NOVA_AUTONOMY: "1",
    NOVA_AUTONOMY_MAX_STEPS: "12",
    NOVA_ALLOW_SHELL: "1",
    NOVA_ALLOW_NETWORK_TOOLS: "1",
    NOVA_MEMORY_SAVE_MODE: "all",
    // Help the renderer find us.
    NOVA_API_BASE: `http://127.0.0.1:${port}`,
  };

  // Ensure these env vars are visible to preload/renderer.
  process.env.NOVA_API_BASE = env.NOVA_API_BASE;

  try {
    backendProc = spawn(exe, [], {
      env,
      stdio: "ignore",
      windowsHide: true,
    });
  } catch (e) {
    backendProc = null;
    // Let the UI still open; it will show connection errors if backend is unavailable.
  }

  return backendProc;
}

function stopBackend() {
  try {
    if (backendProc && !backendProc.killed) backendProc.kill();
  } catch {}
  backendProc = null;
}

function clamp01(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return 0;
  return Math.min(1, Math.max(0, num));
}

function normalizedPointToScreen(webContents, payload) {
  const win = BrowserWindow.fromWebContents(webContents);
  if (!win) return null;

  const bounds = win.getBounds();
  const display = screen.getDisplayNearestPoint({
    x: Math.round(bounds.x + bounds.width / 2),
    y: Math.round(bounds.y + bounds.height / 2),
  }) || screen.getPrimaryDisplay();
  const area = display.workArea || display.bounds;
  const xNorm = clamp01(payload?.x ?? 0);
  const yNorm = clamp01(payload?.y ?? 0);

  return {
    x: Math.round(area.x + area.width * xNorm),
    y: Math.round(area.y + area.height * yNorm),
  };
}

function createMainWindow() {
  const win = new BrowserWindow({
    width: 1280,
    height: 800,
    show: true,
    frame: false,
    titleBarStyle: "hidden",
    backgroundColor: "#05020f",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  // Permissions (mic/camera): in sandboxed Electron, these can be denied by default
  // unless explicitly allowed. We allow only media (audio/video) and geolocation for this app.
  try {
    win.webContents.session.setPermissionRequestHandler((webContents, permission, callback, details) => {
      if (permission === "media") {
        const types = (details && details.mediaTypes) ? details.mediaTypes : [];
        const ok = types.includes("audio") || types.includes("video") || types.length === 0;
        callback(!!ok);
        return;
      }
      if (permission === "geolocation") {
        callback(true);
        return;
      }
      callback(false);
    });

    win.webContents.session.setPermissionCheckHandler((webContents, permission, origin, details) => {
      if (permission === "media") {
        const types = (details && details.mediaTypes) ? details.mediaTypes : [];
        return types.includes("audio") || types.includes("video") || types.length === 0;
      }
      if (permission === "geolocation") {
        return true;
      }
      return false;
    });
  } catch {}

  // Open external links in the system browser
  win.webContents.setWindowOpenHandler(({ url }) => {
    try {
      shell.openExternal(url);
    } catch {}
    return { action: "deny" };
  });

  if (isDev()) {
    const url = process.env.ELECTRON_RENDERER_URL || "http://localhost:5173/";
    win.loadURL(url);
  } else {
    win.loadFile(path.join(__dirname, "..", "dist", "index.html"));
  }
}

app.whenReady().then(async () => {
  resetChromiumCaches();

  const port = await findFreePort(8008);
  startBackendIfPackaged(port);
  await waitForBackendReady(port);

  // Safe, single-purpose IPC: open the user model folder.
  ipcMain.handle("nova:openModelFolder", async () => {
    try {
      const userData = app.getPath("userData");
      const modelDir = path.join(userData, "model");
      try {
        fs.mkdirSync(modelDir, { recursive: true });
      } catch {}
      const err = await shell.openPath(modelDir);
      return { ok: !err, path: modelDir, error: err || null };
    } catch (e) {
      return { ok: false, error: String(e?.message || e) };
    }
  });

  // Screen vision: single-shot capture only, always triggered by an explicit
  // renderer-side user action (a button click) — never periodic or silent.
  // Uses desktopCapturer's thumbnail directly (no getUserMedia/live stream
  // needed for a single frame), so no extra OS capture permission dialog.
  ipcMain.handle("nova:captureScreen", async (event) => {
    try {
      const win = BrowserWindow.fromWebContents(event.sender);
      const display = win
        ? screen.getDisplayNearestPoint({
            x: Math.round(win.getBounds().x + win.getBounds().width / 2),
            y: Math.round(win.getBounds().y + win.getBounds().height / 2),
          })
        : screen.getPrimaryDisplay();
      const size = (display || screen.getPrimaryDisplay()).size;
      const sources = await desktopCapturer.getSources({
        types: ["screen"],
        thumbnailSize: { width: Math.min(1920, size.width), height: Math.min(1080, size.height) },
      });
      if (!sources.length) {
        return { ok: false, error: "No screen source available." };
      }
      // Prefer the display nearest the Nova window when multiple screens exist.
      const match = display && sources.find((s) => String(s.display_id) === String(display.id));
      const source = match || sources[0];
      const dataUrl = source.thumbnail.toDataURL();
      if (!dataUrl || dataUrl === "data:image/png;base64,") {
        return { ok: false, error: "Screen capture returned an empty image." };
      }
      return { ok: true, dataUrl };
    } catch (e) {
      return { ok: false, error: String(e?.message || e) };
    }
  });

  ipcMain.handle("nova:desktopGestureStatus", async () => {
    return {
      ok: true,
      supported: isDesktopInputSupported(),
      platform: process.platform,
    };
  });

  ipcMain.handle("nova:desktopMoveCursor", async (event, payload) => {
    if (!isDesktopInputSupported()) {
      return { ok: false, error: "unsupported_platform" };
    }
    const point = normalizedPointToScreen(event.sender, payload);
    if (!point) {
      return { ok: false, error: "window_not_found" };
    }
    return moveCursor(point.x, point.y);
  });

  ipcMain.handle("nova:desktopClick", async (event, payload) => {
    if (!isDesktopInputSupported()) {
      return { ok: false, error: "unsupported_platform" };
    }
    const point = normalizedPointToScreen(event.sender, payload);
    if (!point) {
      return { ok: false, error: "window_not_found" };
    }
    return leftClick(point.x, point.y);
  });

  ipcMain.handle("nova:desktopMouseDown", async (event, payload) => {
    if (!isDesktopInputSupported()) {
      return { ok: false, error: "unsupported_platform" };
    }
    const point = normalizedPointToScreen(event.sender, payload);
    if (!point) {
      return { ok: false, error: "window_not_found" };
    }
    return leftDown(point.x, point.y);
  });

  ipcMain.handle("nova:desktopMouseUp", async (event, payload) => {
    if (!isDesktopInputSupported()) {
      return { ok: false, error: "unsupported_platform" };
    }
    const point = normalizedPointToScreen(event.sender, payload);
    if (!point) {
      return { ok: false, error: "window_not_found" };
    }
    return leftUp(point.x, point.y);
  });

  ipcMain.handle("nova:windowMinimize", async (event) => {
    const win = BrowserWindow.fromWebContents(event.sender);
    if (!win) return { ok: false, error: "window_not_found" };
    win.minimize();
    return { ok: true };
  });

  ipcMain.handle("nova:windowToggleMaximize", async (event) => {
    const win = BrowserWindow.fromWebContents(event.sender);
    if (!win) return { ok: false, error: "window_not_found" };
    if (win.isMaximized()) win.unmaximize();
    else win.maximize();
    return { ok: true, maximized: win.isMaximized() };
  });

  ipcMain.handle("nova:windowClose", async (event) => {
    const win = BrowserWindow.fromWebContents(event.sender);
    if (!win) return { ok: false, error: "window_not_found" };
    win.close();
    return { ok: true };
  });

  createMainWindow();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createMainWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("before-quit", () => {
  try {
    disposeDesktopInput();
  } catch {}
  stopBackend();
});
