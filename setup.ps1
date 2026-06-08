# NEXUS Agentic OS — First-Time Setup Wizard
# Run once: .\setup.ps1

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Write-Step($n, $total, $msg) {
    Write-Host "`n[$n/$total] $msg..." -ForegroundColor Cyan
}

function Write-OK($msg) { Write-Host "      v $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "      ! $msg" -ForegroundColor Yellow }
function Write-Fail($msg) { Write-Host "      x $msg" -ForegroundColor Red }

function Get-SecureInput($prompt) {
    $secure = Read-Host -AsSecureString $prompt
    $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { return [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

function Set-VaultSecret($key, $value) {
    $env:_NEXUS_SECRET_KEY = $key
    $env:_NEXUS_SECRET_VAL = $value
    & .\venv\Scripts\python.exe -c @"
import os, sys
sys.path.insert(0, '.')
key = os.environ.pop('_NEXUS_SECRET_KEY', '')
val = os.environ.pop('_NEXUS_SECRET_VAL', '')
from backend.secrets.vault import set_secret
set_secret(key, val)
import json, pathlib
from datetime import datetime
meta_path = pathlib.Path('nexus.vault.meta')
meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
if key not in meta: meta[key] = {}
meta[key]['last_set'] = datetime.utcnow().isoformat()
meta_path.write_text(json.dumps(meta, indent=2))
"@
    Remove-Item Env:_NEXUS_SECRET_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:_NEXUS_SECRET_VAL -ErrorAction SilentlyContinue
}

function Test-Credential($key) {
    Write-Host "        -> Testing..." -NoNewline
    $result = & .\venv\Scripts\python.exe -c @"
import asyncio, sys, json
sys.path.insert(0, '.')
async def main():
    from backend.api.secrets import _run_test
    ok, err = await _run_test('$key')
    print(json.dumps({'ok': ok, 'err': err}))
asyncio.run(main())
"@ 2>&1
    try {
        $data = $result | Where-Object { $_ -match '^\{' } | Select-Object -Last 1 | ConvertFrom-Json
        if ($data.ok) { Write-Host " Connected" -ForegroundColor Green; return $true }
        else { Write-Host " $($data.err)" -ForegroundColor Red; return $false }
    } catch {
        Write-Host " Test failed" -ForegroundColor Red
        return $false
    }
}

function Write-EnvLine($key, $value) {
    $content = if (Test-Path ".env") { Get-Content ".env" -Raw } else { "" }
    if ($content -match "(?m)^$key=") {
        $content = $content -replace "(?m)^$key=.*", "$key=$value"
    } else {
        $content += "`n$key=$value"
    }
    Set-Content ".env" $content.Trim()
}

# ── Header ──────────────────────────────────────────────────────────────────
Write-Host "`n======================================================" -ForegroundColor Cyan
Write-Host "  NEXUS Agentic OS — Setup Wizard" -ForegroundColor White
Write-Host "======================================================`n" -ForegroundColor Cyan

# ── [1/7] Prerequisites ──────────────────────────────────────────────────────
Write-Step 1 7 "Checking prerequisites"

$python = $null
foreach ($cmd in @("python3.11", "python3", "python")) {
    try {
        $ver = & $cmd --version 2>&1
        if ($ver -match "3\.(1[1-9]|[2-9]\d)") { $python = $cmd; break }
    } catch {}
}
if (-not $python) { Write-Fail "Python 3.11+ not found — install from python.org"; exit 1 }
Write-OK "$($ver)"

$node = $null
try { $nodeVer = node --version; if ($nodeVer -match "v(1[8-9]|[2-9]\d)") { $node = "node"; Write-OK "Node $nodeVer" } }
catch {}
if (-not $node) { Write-Fail "Node 18+ not found — install from nodejs.org"; exit 1 }

try { $npm = npm --version; Write-OK "npm $npm" } catch { Write-Fail "npm not found"; exit 1 }

# ── [2/7] Python venv ────────────────────────────────────────────────────────
Write-Step 2 7 "Creating Python virtual environment"
if (-not (Test-Path "venv")) {
    & $python -m venv venv
}
Write-OK "venv ready at .\venv"

# ── [3/7] Python dependencies ────────────────────────────────────────────────
Write-Step 3 7 "Installing Python dependencies"
& .\venv\Scripts\pip.exe install -r requirements.txt --quiet
Write-OK "Python packages installed"

# ── [4/7] Frontend dependencies ──────────────────────────────────────────────
Write-Step 4 7 "Installing frontend dependencies"
Push-Location frontend
npm install --silent
Pop-Location
Write-OK "node_modules ready"

# ── [5/7] Vault key ──────────────────────────────────────────────────────────
Write-Step 5 7 "Generating master vault key"
if (-not (Test-Path ".vault.key")) {
    & .\venv\Scripts\python.exe -c @"
import sys; sys.path.insert(0, '.')
from backend.secrets.migrations import generate_vault_key
generate_vault_key()
"@
    attrib +H ".vault.key" 2>$null
    Write-OK ".vault.key created (hidden file)"
} else {
    Write-OK ".vault.key already exists — skipping"
}

# ── [6/7] Configure integrations ─────────────────────────────────────────────
Write-Step 6 7 "Configuring your integrations"

# Copy example .env if missing
if (-not (Test-Path ".env")) {
    if (Test-Path ".env.example") {
        Copy-Item ".env.example" ".env"
    } else {
        Set-Content ".env" ""
    }
}

# ── AI Models ──
Write-Host "`n      ── AI Models ──────────────────────────────────────" -ForegroundColor DarkCyan

$anthropicKey = ""
while ($anthropicKey.Length -lt 10) {
    $anthropicKey = Get-SecureInput "      Anthropic API Key (sk-ant-...)"
    if ($anthropicKey.Length -lt 10) { Write-Fail "Anthropic API key is required. Please enter a valid key." }
}
Set-VaultSecret "ANTHROPIC_API_KEY" $anthropicKey
Test-Credential "ANTHROPIC_API_KEY" | Out-Null

$openrouterKey = Get-SecureInput "      OpenRouter API Key [optional, Enter to skip]"
if ($openrouterKey.Length -gt 5) {
    Set-VaultSecret "OPENROUTER_API_KEY" $openrouterKey
    Write-OK "OpenRouter key saved"
} else { Write-Warn "Skipped OpenRouter" }

# ── Home & Network ──
Write-Host "`n      ── Home & Network ─────────────────────────────────" -ForegroundColor DarkCyan

$hassHost = Read-Host "      Home Assistant host [http://192.168.1.x:8123]"
if (-not $hassHost) { $hassHost = "http://192.168.1.x:8123" }
Write-EnvLine "HASS_HOST" $hassHost

$hassToken = Get-SecureInput "      Home Assistant token"
if ($hassToken.Length -gt 5) {
    Set-VaultSecret "HASS_TOKEN" $hassToken
    Test-Credential "HASS_TOKEN" | Out-Null
}

$unifiHost = Read-Host "      UniFi host [https://192.168.1.1]"
if (-not $unifiHost) { $unifiHost = "https://192.168.1.1" }
Write-EnvLine "UNIFI_HOST" $unifiHost

$unifiUser = Read-Host "      UniFi username [admin]"
if (-not $unifiUser) { $unifiUser = "admin" }
Write-EnvLine "UNIFI_USERNAME" $unifiUser

$unifiPass = Get-SecureInput "      UniFi password"
if ($unifiPass.Length -gt 0) { Set-VaultSecret "UNIFI_PASSWORD" $unifiPass }

$unraidHost = Read-Host "      Unraid host [192.168.1.x]"
if (-not $unraidHost) { $unraidHost = "192.168.1.x" }
Write-EnvLine "UNRAID_HOST" $unraidHost

$unraidKey = Get-SecureInput "      Unraid API key"
if ($unraidKey.Length -gt 0) { Set-VaultSecret "UNRAID_API_KEY" $unraidKey }

$adguardHost = Read-Host "      AdGuard host [http://192.168.1.x:3000]"
if (-not $adguardHost) { $adguardHost = "http://192.168.1.x:3000" }
Write-EnvLine "ADGUARD_HOST" $adguardHost

$adguardUser = Read-Host "      AdGuard username [admin]"
if (-not $adguardUser) { $adguardUser = "admin" }
Write-EnvLine "ADGUARD_USER" $adguardUser

$adguardPass = Get-SecureInput "      AdGuard password"
if ($adguardPass.Length -gt 0) { Set-VaultSecret "ADGUARD_PASS" $adguardPass }

# ── Media ──
Write-Host "`n      ── Media ──────────────────────────────────────────" -ForegroundColor DarkCyan
$channelsHost = Read-Host "      Channels DVR host [http://192.168.1.x:8089]"
if (-not $channelsHost) { $channelsHost = "http://192.168.1.x:8089" }
Write-EnvLine "CHANNELS_HOST" $channelsHost

# ── Developer ──
Write-Host "`n      ── Developer ──────────────────────────────────────" -ForegroundColor DarkCyan
$githubToken = Get-SecureInput "      GitHub token (ghp_...)"
if ($githubToken.Length -gt 5) { Set-VaultSecret "GITHUB_TOKEN" $githubToken }

$githubUser = Read-Host "      GitHub username"
if ($githubUser) { Write-EnvLine "GITHUB_USERNAME" $githubUser }

# ── Notes ──
Write-Host "`n      ── Notes ──────────────────────────────────────────" -ForegroundColor DarkCyan
$obsidianHost = Read-Host "      Obsidian REST API host [http://localhost:27123]"
if (-not $obsidianHost) { $obsidianHost = "http://localhost:27123" }
Write-EnvLine "OBSIDIAN_HOST" $obsidianHost

$obsidianToken = Get-SecureInput "      Obsidian token"
if ($obsidianToken.Length -gt 0) { Set-VaultSecret "OBSIDIAN_TOKEN" $obsidianToken }

# ── Weather ──
Write-Host "`n      ── Weather ────────────────────────────────────────" -ForegroundColor DarkCyan
$weatherKey = Get-SecureInput "      OpenWeatherMap API key"
if ($weatherKey.Length -gt 0) { Set-VaultSecret "OPENWEATHER_API_KEY" $weatherKey }

$weatherLat = Read-Host "      Your latitude [42.33]"
if (-not $weatherLat) { $weatherLat = "42.33" }
Write-EnvLine "WEATHER_LAT" $weatherLat

$weatherLon = Read-Host "      Your longitude [-83.04]"
if (-not $weatherLon) { $weatherLon = "-83.04" }
Write-EnvLine "WEATHER_LON" $weatherLon

# ── Agent Bridge ──
Write-Host "`n      ── Agent Bridge ───────────────────────────────────" -ForegroundColor DarkCyan
$hermesHost = Read-Host "      Hermes host [http://192.168.1.x:PORT]"
if ($hermesHost) { Write-EnvLine "HERMES_HOST" $hermesHost }

$hermesSecret = Get-SecureInput "      Hermes webhook secret"
if ($hermesSecret.Length -gt 0) { Set-VaultSecret "HERMES_WEBHOOK_SECRET" $hermesSecret }

# ── NEXUS System ──
Write-Host "`n      ── NEXUS System ───────────────────────────────────" -ForegroundColor DarkCyan
$briefingTime = Read-Host "      Briefing time (24h) [07:00]"
if (-not $briefingTime) { $briefingTime = "07:00" }
Write-EnvLine "BRIEFING_TIME" $briefingTime

$timezone = Read-Host "      Timezone [America/Detroit]"
if (-not $timezone) { $timezone = "America/Detroit" }
Write-EnvLine "BRIEFING_TIMEZONE" $timezone

$memoFolder = Read-Host "      Voice memo watch folder [./watched_memos]"
if (-not $memoFolder) { $memoFolder = "./watched_memos" }
Write-EnvLine "MEMO_WATCH_FOLDER" $memoFolder

$whisperMode = Read-Host "      Whisper mode (local/api) [local]"
if ($whisperMode -eq "api") { Write-EnvLine "WHISPER_API" "true" } else { Write-EnvLine "WHISPER_API" "false" }

# Generate NEXUS API key
Write-Host "        -> Generating NEXUS API key..." -NoNewline
$nexusKey = & .\venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
Set-VaultSecret "NEXUS_API_KEY" $nexusKey
Write-Host " (stored in vault)" -ForegroundColor Green

# ── [7/7] Build frontend ─────────────────────────────────────────────────────
Write-Step 7 7 "Building frontend"
Push-Location frontend
npm run build --silent
Pop-Location
Write-OK "Production build complete"

# ── Summary ──────────────────────────────────────────────────────────────────
$secretCount = & .\venv\Scripts\python.exe -c @"
import sys; sys.path.insert(0, '.')
from backend.secrets.vault import list_keys
print(len(list_keys()))
"@

Write-Host "`n======================================================" -ForegroundColor Cyan
Write-Host "  NEXUS setup complete!" -ForegroundColor White
Write-Host ""
Write-Host "  Secrets stored in vault: $secretCount" -ForegroundColor Gray
Write-Host ""
Write-Host "  IMPORTANT: Back up these two files:" -ForegroundColor Yellow
Write-Host "    nexus.vault  <- your encrypted secrets" -ForegroundColor Yellow
Write-Host "    .vault.key   <- your master key (keep separate!)" -ForegroundColor Yellow
Write-Host ""
Write-Host "  To start NEXUS: .\start.ps1" -ForegroundColor Green
Write-Host "======================================================`n" -ForegroundColor Cyan
