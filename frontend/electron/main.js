const { app, BrowserWindow, shell } = require("electron");
const path = require("path");

// Allow programmatic audio playback (TTS) without requiring a user gesture.
// This is particularly important because TTS playback happens after async fetch.
try {
  app.commandLine.appendSwitch("autoplay-policy", "no-user-gesture-required");
} catch {}

function isDev() {
  return !app.isPackaged;
}

function createMainWindow() {
  const win = new BrowserWindow({
    width: 1280,
    height: 800,
    show: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

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

app.whenReady().then(() => {
  createMainWindow();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createMainWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
