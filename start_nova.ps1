param(
  [switch]$Test,
  [string]$NovaHost = "127.0.0.1",
  [int]$BackendPort = 8008,
  [int]$VitePort = 5173
)

$ErrorActionPreference = "Stop"

function Fail([string]$msg) {
  Write-Host ""
  Write-Host "ERROR: $msg" -ForegroundColor Red
  Read-Host "Press Enter to close"
  exit 1
}

function Is-PortFree([int]$p) {
  try {
    $c = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
    return ($null -eq $c)
  } catch {
    # On older systems, Get-NetTCPConnection may be unavailable; assume free.
    return $true
  }
}

$repoRoot = (Resolve-Path $PSScriptRoot).Path
$frontendDir = Join-Path $repoRoot "frontend"
$backendDir  = Join-Path $repoRoot "backend"
$modelDir    = Join-Path $repoRoot "model"
$venvPython  = Join-Path $repoRoot "venv\Scripts\python.exe"

Write-Host "Nova repo root: $repoRoot" -ForegroundColor Cyan
Write-Host "Backend dir:    $backendDir"
Write-Host "Frontend dir:   $frontendDir"
Write-Host "Model dir:      $modelDir"
Write-Host "Python (venv):  $venvPython"
Write-Host "Host/Port:      ${NovaHost}:${BackendPort}"
Write-Host ""

if (!(Test-Path $backendDir))  { Fail "Missing backend directory: $backendDir" }
if (!(Test-Path $frontendDir)) { Fail "Missing frontend directory: $frontendDir" }
if (!(Test-Path (Join-Path $frontendDir "package.json"))) { Fail "Missing frontend\package.json" }
if (!(Test-Path $modelDir))    { Fail "Missing model directory: $modelDir" }
if (!(Test-Path $venvPython))  { Fail "Missing venv python: $venvPython (recreate with: python -m venv venv)" }

# Pick a GGUF (your backend also does this at startup, but we validate early for clarity)
$gguf = Get-ChildItem -Path $modelDir -Filter *.gguf -File -ErrorAction SilentlyContinue |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1

if (-not $gguf) { Fail "No .gguf found in model\. Put any .gguf file in: $modelDir" }
Write-Host "Detected GGUF: $($gguf.Name) (auto-pick = most recently modified)" -ForegroundColor Green

# Choose Vite port: prefer VitePort, bump if busy
$rendererPort = $VitePort
for ($i=0; $i -lt 20; $i++) {
  if (Is-PortFree $rendererPort) { break }
  $rendererPort++
}
$rendererUrl = "http://localhost:$rendererPort/"
Write-Host "Renderer URL:  $rendererUrl" -ForegroundColor Green
Write-Host ""

# --- AUTONOMY DEFAULTS ---
# You asked Nova to be fully autonomous by default.
$env:NOVA_AUTONOMY = "1"
$env:NOVA_AUTONOMY_MAX_STEPS = "12"
$env:NOVA_ALLOW_SHELL = "1"
$env:NOVA_ALLOW_NETWORK_TOOLS = "1"
$env:NOVA_MEMORY_SAVE_MODE = "all"

if ($Test) {
  Write-Host "Test mode enabled: not starting processes." -ForegroundColor Yellow
  exit 0
}

# --- BACKEND ---
if (-not (Is-PortFree $BackendPort)) {
  Write-Host "WARNING: Port $BackendPort is already in use. Backend may fail to start (EADDRINUSE)." -ForegroundColor Yellow
}
Write-Host "Starting backend..." -ForegroundColor Cyan

# IMPORTANT: use venv python explicitly so uvicorn and llama-cpp-python are the ones you just installed.
Start-Process powershell -WindowStyle Normal -ArgumentList @(
  "-NoExit",
  "-Command",
  "cd `"$repoRoot`"; `"$venvPython`" -m uvicorn backend.app:app --host $NovaHost --port $BackendPort --log-level warning --no-access-log"
)

# --- FRONTEND (Vite) ---
Write-Host "Starting frontend (Vite)..." -ForegroundColor Cyan
Start-Process powershell -WindowStyle Normal -ArgumentList @(
  "-NoExit",
  "-Command",
  "cd `"$frontendDir`"; if (!(Test-Path node_modules)) { npm install }; npm run dev -- --port $rendererPort --strictPort"
)

# Give Vite a moment to bind its port before Electron opens.
Start-Sleep -Seconds 1

# --- ELECTRON ---
# IMPORTANT: Quote ELECTRON_RENDERER_URL so PowerShell does NOT treat the URL as a command.
Write-Host "Starting Electron..." -ForegroundColor Cyan
Start-Process powershell -WindowStyle Normal -ArgumentList @(
  "-NoExit",
  "-Command",
  "cd `"$frontendDir`"; `$env:ELECTRON_RENDERER_URL=`"$rendererUrl`"; npx electron .\electron\main.js; Write-Host 'Electron exited. Press Enter to close.'; Read-Host | Out-Null"
)

Write-Host ""
Write-Host "Launched: backend + vite + electron" -ForegroundColor Green
Write-Host "If the UI spins on 'thinking', watch the BACKEND window for tracebacks." -ForegroundColor Yellow
