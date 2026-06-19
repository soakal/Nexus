# Brain Organizer — daily processing wrapper
# Called by NEXUS scheduler. Activates the venv and runs brain_organizer.py.
# Exit code is forwarded to NEXUS (non-zero = retry eligible).

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $ScriptDir "venv\Scripts\python.exe"
$Script     = Join-Path $ScriptDir "brain_organizer.py"

if (-not (Test-Path $VenvPython)) {
    Write-Error "Python venv not found at $VenvPython — run setup first."
    exit 1
}

if (-not (Test-Path $Script)) {
    Write-Error "brain_organizer.py not found at $Script"
    exit 1
}

# NEXUS secrets vault injects these before calling this script
# ANTHROPIC_API_KEY, HERMES_HOST are expected in the environment

& $VenvPython $Script
exit $LASTEXITCODE
