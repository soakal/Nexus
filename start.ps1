# NEXUS Agentic OS - Start
# Usage: .\start.ps1 [-dev] [-port 3000]
param([switch]$dev, [int]$port = 3000)

$ErrorActionPreference = "Stop"

# --- Single-instance guard ---------------------------------------------------
# Two overlapping start.ps1 runs used to race: one invocation's port-kill could
# kill the OTHER invocation's freshly-started backend, leaving :8000 down. A
# named mutex makes a second concurrent run abort cleanly instead of racing.
# The OS releases the mutex automatically when this process exits (any path), so
# it only guards the kill+start *sequence*, not the running app's lifetime.
$startMutex = New-Object System.Threading.Mutex($false, "Global\NEXUS_START_LOCK")
$acquired = $false
try {
    $acquired = $startMutex.WaitOne([TimeSpan]::FromSeconds(2))
} catch [System.Threading.AbandonedMutexException] {
    # A previous start died holding the lock - we now own it. Proceed.
    $acquired = $true
}
if (-not $acquired) {
    Write-Host "Another NEXUS start/restart is already in progress - aborting this one." -ForegroundColor Yellow
    exit 0
}

if (-not (Test-Path ".vault.key")) {
    if (-not (Test-Path "venv\Scripts\python.exe")) {
        Write-Host "ERROR: venv not found. Run .\setup.ps1 first to install dependencies." -ForegroundColor Red
        exit 1
    }
    Write-Host "First run - generating vault key..." -ForegroundColor Cyan
    & .\venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; open('.vault.key','wb').write(Fernet.generate_key())"
    if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: Failed to generate vault key." -ForegroundColor Red; exit 1 }
    attrib +H ".vault.key" 2>$null
    Write-Host "Open http://localhost:$port to complete setup." -ForegroundColor Yellow
}
# nexus.vault is created by the setup wizard on first secret write

Write-Host "Starting NEXUS..." -ForegroundColor Cyan

# Kill any existing NEXUS processes on these ports (PID file first, then port owners)
if (Test-Path ".nexus.pids") {
    try {
        $oldPids = Get-Content ".nexus.pids" | ConvertFrom-Json
        foreach ($id in @($oldPids.backend, $oldPids.frontend)) {
            if ($id) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }
        }
    } catch {}
}
@(8000, $port) | ForEach-Object {
    $p = $_
    try {
        $conn = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
        if ($conn) {
            Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue
        }
    } catch {}
}
# Wait until both ports are actually FREE before starting - otherwise we'd launch
# the new backend while the old one is still releasing the socket (the race that
# left :8000 down). Bounded to ~5s per port.
foreach ($p in @(8000, $port)) {
    for ($i = 0; $i -lt 20; $i++) {
        $still = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
        if (-not $still) { break }
        Start-Sleep -Milliseconds 250
    }
}

# Start backend
# Redirect stdout/stderr to log files so startup failures are visible.
$backendLog = ".\logs\backend.log"
$backendErr = ".\logs\backend.err.log"
New-Item -ItemType Directory -Force -Path ".\logs" | Out-Null
# Launch via run.py (NOT `-m uvicorn`): run.py blocking-prewarms the shared TLS
# context (see run.py) on the bare main thread before uvicorn/its event loop
# even exist, instead of stalling live requests later.
$backendArgs = "run.py"
if ($dev) { $backendArgs += " --reload" }
$backend = Start-Process -PassThru -WindowStyle Hidden `
    -FilePath ".\venv\Scripts\python.exe" -ArgumentList $backendArgs `
    -WorkingDirectory (Get-Location).Path `
    -RedirectStandardOutput $backendLog -RedirectStandardError $backendErr

# Wait for backend health check.
# NOTE: use 127.0.0.1 (not "localhost"). On Windows, "localhost" resolves to
# IPv6 ::1 first, but uvicorn binds --host 0.0.0.0 (IPv4 only), so a "localhost"
# probe hangs until timeout on every iteration and never succeeds.
$ready = $false
Write-Host "  Waiting for backend..." -NoNewline
# ~60s budget: absorbs a worst-case single Infisical timeout (~10-20s inside
# lifespan) during a vault-fallback cold start without cutting a slow-but-
# successful startup short. Loop still exits immediately on success.
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep 1
    # Fail fast if the backend process died during startup.
    if ($backend.HasExited) {
        Write-Host ""
        Write-Host "ERROR: Backend process exited (code $($backend.ExitCode)) during startup." -ForegroundColor Red
        if (Test-Path $backendErr) {
            Write-Host "--- backend.err.log ---" -ForegroundColor DarkGray
            Get-Content $backendErr -Tail 30 | ForEach-Object { Write-Host $_ -ForegroundColor DarkGray }
        }
        exit 1
    }
    try {
        $r = Invoke-RestMethod "http://127.0.0.1:8000/api/health" -TimeoutSec 8 -ErrorAction SilentlyContinue
        if ($r.status -in @("ok", "vault_empty")) { $ready = $true; break }
    } catch {}
    Write-Host "." -NoNewline
}
Write-Host ""

if (-not $ready) {
    Write-Host "ERROR: Backend failed to start. See $backendErr" -ForegroundColor Red
    if (Test-Path $backendErr) {
        Write-Host "--- backend.err.log (last 30 lines) ---" -ForegroundColor DarkGray
        Get-Content $backendErr -Tail 30 | ForEach-Object { Write-Host $_ -ForegroundColor DarkGray }
    }
    Stop-Process -Id $backend.Id -Force -ErrorAction SilentlyContinue
    exit 1
}
Write-Host "  Backend ready (pid $($backend.Id))" -ForegroundColor Green

# Start frontend
if ($dev) {
    $frontend = Start-Process -PassThru -WindowStyle Hidden `
        -FilePath "cmd.exe" `
        -ArgumentList "/c cd /d `"$(Get-Location)\frontend`" && npm run dev -- --port $port" `
        -WorkingDirectory (Get-Location).Path
} else {
    # Serve the production build with vite preview (handles SPA routing)
    $frontend = Start-Process -PassThru -WindowStyle Hidden `
        -FilePath "cmd.exe" `
        -ArgumentList "/c cd /d `"$(Get-Location)\frontend`" && npx vite preview --port $port --host 0.0.0.0" `
        -WorkingDirectory (Get-Location).Path
}

# Wait for the frontend to actually LISTEN on $port before reporting ready.
# This used to be a blind `Start-Sleep -Seconds 2` followed by an unconditional
# "Frontend ready". That was a lie: tray.py's _backend_healthy() requires a TCP
# connect to 127.0.0.1:3000 on top of /api/health, so whenever the frontend took
# longer than 2s to bind, start.ps1 exited 0 -> tray flipped to "running" ->
# its 15s monitor ticks failed twice -> "Backend went unhealthy while running"
# -> a pointless full auto-restart of a perfectly healthy NEXUS, ~30s after every
# cold start (see logs/tray.log 2026-08-12 19:26:58/19:27:15 and 20:03:01/20:03:33).
# `npx vite preview` is a 4-process chain (cmd -> npx node -> cmd -> vite node) and
# measured ~3.8s warm on this host; cold after boot it has taken well over 30s.
# Bounded to ~60s. If it never binds we still exit 0 on purpose: the backend IS up,
# and a non-zero exit parks the tray at "stopped" with no auto-recovery (the monitor
# only auto-restarts a backend that was "running"), which is strictly worse than
# letting the tray's own watchdog handle a genuinely dead frontend.
$feReady = $false
Write-Host "  Waiting for frontend..." -NoNewline
for ($i = 0; $i -lt 120; $i++) {
    Start-Sleep -Milliseconds 500
    if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
        $feReady = $true
        break
    }
    if ($frontend.HasExited) { break }
    if ($i % 4 -eq 3) { Write-Host "." -NoNewline }
}
Write-Host ""
if ($feReady) {
    Write-Host "  Frontend ready (pid $($frontend.Id))" -ForegroundColor Green
} else {
    Write-Host "  WARNING: frontend never started listening on port $port (pid $($frontend.Id), exited=$($frontend.HasExited)). Backend is up - continuing anyway." -ForegroundColor Yellow
}


Write-Host ""
Write-Host "NEXUS is running" -ForegroundColor Green
Write-Host "  Dashboard : http://localhost:$port" -ForegroundColor Cyan
Write-Host "  API       : http://localhost:8000" -ForegroundColor Cyan
Write-Host "  Backend PID: $($backend.Id)  Frontend PID: $($frontend.Id)"
Write-Host ""
Write-Host "To stop: .\stop.ps1" -ForegroundColor DarkGray

# Save PIDs for stop.ps1
@{ backend = $backend.Id; frontend = $frontend.Id } | ConvertTo-Json | Set-Content ".nexus.pids"
