# Task Scheduler action target for the "NEXUS Tray" task.
#
# Why this file exists: the venv's pythonw.exe is a launcher STUB for
# windowless/GUI-subsystem apps -- it starts the real interpreter as a
# detached child and exits immediately, without waiting on it. Task
# Scheduler only ever sees that stub, so a direct
# "Execute: pythonw.exe, Arguments: tray.py" action reports the task as
# finished within ~1s regardless of whether tray.py itself is still
# running.
#
# Also load-bearing: Windows Task Scheduler's "Restart the task on
# failure" setting does NOT fire on a non-zero exit code from a
# successfully-launched action -- confirmed empirically 2026-07-27
# (Event 201 logged "successfully completed" despite exit 1, no retry
# ever queued) and independently confirmed via Microsoft's own forum
# guidance: RestartOnFailure only applies if the scheduler fails to
# START the action at all, never to what the action does afterward.
# So this script does NOT exit and rely on Task Scheduler to relaunch
# it -- it loops forever itself. Task Scheduler's job is reduced to:
# start this once at logon, plus a 15-minute repetition on the trigger
# as a backstop in case the supervisor process itself ever dies.
#
# Also load-bearing: tray.py's own main() calls
# _kill_other_tray_instances() BEFORE acquire_single_instance() -- a
# naive relaunch that doesn't check for an already-running tray first
# would kill a perfectly healthy tray and steal its place. The
# Get-TrayProc check below exists specifically to avoid that.

$repo       = $PSScriptRoot
$pythonw    = Join-Path $repo "venv\Scripts\pythonw.exe"
$trayScript = Join-Path $repo "tray.py"
$logPath    = Join-Path $repo "logs\tray_supervisor.log"

function Get-TrayProc {
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -match [regex]::Escape($trayScript) }
}

function Write-Log([string]$m) {
    "{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m | Add-Content -LiteralPath $logPath
}

Write-Log "supervisor started (pid $PID)"
$fastFails = 0

while ($true) {
    if (Get-TrayProc) {
        Write-Log "tray.py already running -- attaching, not relaunching"
    } else {
        Start-Process -FilePath $pythonw -ArgumentList "`"$trayScript`"" -WorkingDirectory $repo
        Write-Log "launched tray.py"
    }

    $started = Get-Date
    Start-Sleep -Seconds 10   # grace period for the stub->real-child handoff
    while (Get-TrayProc) { Start-Sleep -Seconds 5 }

    $lived = ((Get-Date) - $started).TotalSeconds
    Write-Log ("tray.py exited after {0:N0}s" -f $lived)

    if ($lived -lt 60) { $fastFails++ } else { $fastFails = 0 }
    if ($fastFails -ge 5) {
        Write-Log "5 consecutive fast failures -- backing off 10 min"
        Start-Sleep -Seconds 600
        $fastFails = 0
    } else {
        Start-Sleep -Seconds 5
    }
}
