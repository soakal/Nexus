# NEXUS Agentic OS — Clean Shutdown

$ourSessionId = (Get-Process -Id $PID).SessionId
$failed = $false

function Confirm-Killed {
    param([int]$TargetId, [string]$Context)
    for ($i = 0; $i -lt 6; $i++) {
        $p = Get-Process -Id $TargetId -ErrorAction SilentlyContinue
        if (-not $p) { return $true }
        Start-Sleep -Milliseconds 500
    }
    $survivor = Get-Process -Id $TargetId -ErrorAction SilentlyContinue
    if ($survivor) {
        $script:failed = $true
        Write-Host "ERROR: PID $TargetId ($Context) survived kill — it is in Session $($survivor.SessionId) (ours is $ourSessionId)." -ForegroundColor Red
        if ($survivor.SessionId -ne $ourSessionId) {
            Write-Host "        A non-elevated shell cannot terminate a cross-session process. Re-run stop.ps1 from an elevated PowerShell." -ForegroundColor Red
        }
    }
    return $false
}

# Try PID file first
if (Test-Path ".nexus.pids") {
    $pids = Get-Content ".nexus.pids" | ConvertFrom-Json
    foreach ($id in @($pids.backend, $pids.frontend)) {
        if ($id) {
            try { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue } catch {}
            if (Confirm-Killed -TargetId $id -Context "from .nexus.pids") {
                Write-Host "Stopped PID $id" -ForegroundColor Yellow
            }
        }
    }
    Remove-Item ".nexus.pids" -ErrorAction SilentlyContinue
}

# Also kill by port as fallback
@(8000, 3000) | ForEach-Object {
    $p = $_
    try {
        $conn = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
        if ($conn) {
            $targetId = $conn.OwningProcess
            Stop-Process -Id $targetId -Force -ErrorAction SilentlyContinue
            if (Confirm-Killed -TargetId $targetId -Context "port $p") {
                Write-Host "Stopped process on port $p" -ForegroundColor Yellow
            }
        }
    } catch {}
}

if ($failed) {
    Write-Host "NEXUS NOT fully stopped." -ForegroundColor Red
    exit 1
} else {
    Write-Host "NEXUS stopped." -ForegroundColor Cyan
    exit 0
}
