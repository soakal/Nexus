# NEXUS Agentic OS — Clean Shutdown

# Try PID file first
if (Test-Path ".nexus.pids") {
    $pids = Get-Content ".nexus.pids" | ConvertFrom-Json
    foreach ($id in @($pids.backend, $pids.frontend)) {
        if ($id) {
            try { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue; Write-Host "Stopped PID $id" -ForegroundColor Yellow } catch {}
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
            Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue
            Write-Host "Stopped process on port $p" -ForegroundColor Yellow
        }
    } catch {}
}

Write-Host "NEXUS stopped." -ForegroundColor Cyan
