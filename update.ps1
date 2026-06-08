# NEXUS Agentic OS — Update (run monthly or after pulling changes)
Write-Host "Updating NEXUS..." -ForegroundColor Cyan

# Stop if running
if (Test-Path ".\stop.ps1") { .\stop.ps1 }

# Update Python deps
Write-Host "Updating Python packages..."
& .\venv\Scripts\pip.exe install -r requirements.txt --upgrade --quiet
Write-Host "  Python packages updated" -ForegroundColor Green

# Update frontend deps + rebuild
Write-Host "Updating frontend..."
Push-Location frontend
npm install --silent
npm run build --silent
Pop-Location
Write-Host "  Frontend rebuilt" -ForegroundColor Green

Write-Host ""
Write-Host "Update complete." -ForegroundColor Green
Write-Host "Run .\start.ps1 to restart." -ForegroundColor Cyan
