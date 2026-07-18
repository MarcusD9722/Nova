const { spawn } = require("child_process");

function isSupported() {
  return process.platform === "win32";
}

function toInt(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return 0;
  return Math.round(num);
}

class WindowsInputBridge {
  constructor() {
    this._proc = null;
    this._buffer = "";
    this._pending = new Map();
    this._seq = 0;
  }

  _ensureProcess() {
    if (this._proc) return;

    const proc = spawn(
      "powershell.exe",
      ["-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", "-"],
      {
        stdio: ["pipe", "pipe", "pipe"],
        windowsHide: true,
      }
    );

    proc.stdout.setEncoding("utf8");
    proc.stderr.setEncoding("utf8");

    proc.stdout.on("data", (chunk) => {
      this._buffer += String(chunk || "");
      const lines = this._buffer.split(/\r?\n/);
      this._buffer = lines.pop() || "";

      for (const rawLine of lines) {
        const line = String(rawLine || "").trim();
        if (!line) continue;
        if (line.startsWith("__NOVA_OK__")) {
          const id = line.slice("__NOVA_OK__".length);
          const pending = this._pending.get(id);
          if (pending) {
            this._pending.delete(id);
            pending.resolve();
          }
          continue;
        }
        if (line.startsWith("__NOVA_ERR__")) {
          const payload = line.slice("__NOVA_ERR__".length);
          const sep = payload.indexOf("::");
          const id = sep >= 0 ? payload.slice(0, sep) : payload;
          const message = sep >= 0 ? payload.slice(sep + 2) : "desktop_input_failed";
          const pending = this._pending.get(id);
          if (pending) {
            this._pending.delete(id);
            pending.reject(new Error(message || "desktop_input_failed"));
          }
        }
      }
    });

    proc.on("exit", () => {
      const error = new Error("desktop_input_process_exited");
      for (const pending of this._pending.values()) {
        pending.reject(error);
      }
      this._pending.clear();
      this._proc = null;
      this._buffer = "";
    });

    proc.stdin.write(`$ErrorActionPreference = 'Stop'\n`);
    proc.stdin.write(`Add-Type -TypeDefinition @"\nusing System;\nusing System.Runtime.InteropServices;\npublic static class NovaDesktopInput {\n  [DllImport(\"user32.dll\")] public static extern bool SetCursorPos(int X, int Y);\n  [DllImport(\"user32.dll\")] public static extern void mouse_event(uint dwFlags, uint dx, uint dy, uint dwData, UIntPtr dwExtraInfo);\n}\n"@;\n`);

    this._proc = proc;
  }

  invoke(scriptBody) {
    this._ensureProcess();
    const id = String(++this._seq);

    return new Promise((resolve, reject) => {
      this._pending.set(id, { resolve, reject });
      this._proc.stdin.write(`try {\n${scriptBody}\nWrite-Output \"__NOVA_OK__${id}\"\n} catch {\n  Write-Output \"__NOVA_ERR__${id}::$($_.Exception.Message)\"\n}\n`);
    });
  }

  dispose() {
    try {
      this._proc?.kill();
    } catch {}
    this._proc = null;
  }
}

const BRIDGE = new WindowsInputBridge();

async function moveCursor(x, y) {
  if (!isSupported()) {
    return { ok: false, error: "unsupported_platform" };
  }
  const px = toInt(x);
  const py = toInt(y);
  await BRIDGE.invoke(`[NovaDesktopInput]::SetCursorPos(${px}, ${py}) | Out-Null`);
  return { ok: true, x: px, y: py };
}

async function leftDown(x, y) {
  if (!isSupported()) {
    return { ok: false, error: "unsupported_platform" };
  }
  const px = toInt(x);
  const py = toInt(y);
  await BRIDGE.invoke(`[NovaDesktopInput]::SetCursorPos(${px}, ${py}) | Out-Null\n[NovaDesktopInput]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)`);
  return { ok: true, x: px, y: py };
}

async function leftUp(x, y) {
  if (!isSupported()) {
    return { ok: false, error: "unsupported_platform" };
  }
  const px = toInt(x);
  const py = toInt(y);
  await BRIDGE.invoke(`[NovaDesktopInput]::SetCursorPos(${px}, ${py}) | Out-Null\n[NovaDesktopInput]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)`);
  return { ok: true, x: px, y: py };
}

async function leftClick(x, y) {
  if (!isSupported()) {
    return { ok: false, error: "unsupported_platform" };
  }
  const px = toInt(x);
  const py = toInt(y);
  await BRIDGE.invoke(`[NovaDesktopInput]::SetCursorPos(${px}, ${py}) | Out-Null\n[NovaDesktopInput]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)\n[NovaDesktopInput]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)`);
  return { ok: true, x: px, y: py };
}

module.exports = {
  isSupported,
  moveCursor,
  leftDown,
  leftUp,
  leftClick,
  dispose: () => BRIDGE.dispose(),
};