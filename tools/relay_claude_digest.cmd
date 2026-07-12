@echo off
REM Pulls the latest digest commit from the cloud routine, then relays any
REM new digest file into the Brain vault + Telegram. Run daily by a Windows
REM Scheduled Task ("NEXUS Claude Digest Relay"), ~15 min after the cloud
REM routine's 09:00 America/New_York run.
cd /d "C:\Users\Brian\Documents\Agentic os\nexus"
git pull origin master
"venv\Scripts\python.exe" "tools\relay_claude_digest.py"
