# NEXUS database restore - thin orchestrator around backend.agents.backup.restore_from.
# Usage:
#   .\restore.ps1                          # restore from the newest local backups\<ts>\
#   .\restore.ps1 -From "backups\20260702-033000"
#   .\restore.ps1 -From "\\192.168.1.50\Computer Backup\Nexus_backup"
# Stops NEXUS, restores nexus.db (refusing a missing/corrupt backup BEFORE
# stopping anything is validated first), deletes stale WAL sidecars, restarts.
param(
    [string]$From = ""
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not $From) {
    $latest = Get-ChildItem -Directory "backups" -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^\d{8}-\d{6}$' } |
        Sort-Object Name -Descending | Select-Object -First 1
    if (-not $latest) {
        Write-Host "No local backups found under backups\ - pass -From explicitly." -ForegroundColor Red
        exit 1
    }
    $From = $latest.FullName
}

Write-Host "Restore source: $From"

# Validate the backup BEFORE stopping NEXUS (fail fast, don't leave it down).
$check = & .\venv\Scripts\python.exe -c "import sys; sys.path.insert(0, '.'); from backend.agents.backup import integrity_check_file; import os; p = os.path.join(r'$From', 'nexus.db'); print('ok' if os.path.isfile(p) and integrity_check_file(p) == 'ok' else 'bad')"
if ($check -ne "ok") {
    Write-Host "Backup at $From is missing or fails integrity check - aborting before stopping NEXUS." -ForegroundColor Red
    exit 1
}

$confirm = Read-Host "This OVERWRITES the live nexus.db. Type RESTORE to proceed"
if ($confirm -ne "RESTORE") {
    Write-Host "Aborted."
    exit 1
}

.\stop.ps1

$result = & .\venv\Scripts\python.exe -c "import sys, json; sys.path.insert(0, '.'); from backend.agents.backup import restore_from; print(json.dumps(restore_from(r'$From')))"
Write-Host "Restore result: $result"
if ($result -notmatch '"ok": true') {
    Write-Host "Restore FAILED - NEXUS left stopped. Investigate before restarting." -ForegroundColor Red
    exit 1
}

.\start.ps1
Write-Host "Restore complete. Powered by CwiAI" -ForegroundColor Green
