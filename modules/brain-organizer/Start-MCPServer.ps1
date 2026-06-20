# Brain Organizer MCP Server — startup wrapper
# Called by NEXUS on startup. Activates the venv and starts mcp_server.py as a
# background process. Writes the PID to stdout so NEXUS can monitor it.

$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $ScriptDir "venv\Scripts\python.exe"
$Script     = Join-Path $ScriptDir "mcp_server.py"

if (-not (Test-Path $VenvPython)) {
    Write-Error "Python venv not found at $VenvPython — run setup first."
    exit 1
}

if (-not (Test-Path $Script)) {
    Write-Error "mcp_server.py not found at $Script"
    exit 1
}

$proc = Start-Process `
    -FilePath    $VenvPython `
    -ArgumentList $Script `
    -PassThru `
    -WindowStyle Hidden

if ($null -eq $proc) {
    Write-Error "Failed to start MCP server process"
    exit 1
}

# Return PID on stdout for NEXUS health monitoring
Write-Output $proc.Id
exit 0
