@echo off
REM Pulls the latest digest commit from the cloud routine, then relays any
REM new digest file into the Brain vault + Telegram. Run daily by a Windows
REM Scheduled Task ("NEXUS Claude Digest Relay"), ~15 min after the cloud
REM routine's 09:00 America/New_York run.
REM NOTE: the digest routine now lands its file on a digest/YYYY-MM-DD
REM branch + PR instead of committing straight to master (see
REM DIGEST_INSTRUCTIONS.md), so `git pull origin master` below legitimately
REM brings down nothing on a day a PR is still open/unmerged. The Python
REM script queries origin directly for pending `digest/*` branches and
REM prints a distinct notice rather than a plain "nothing new to relay" in
REM that case -- read its output, don't just check the exit code.
cd /d "C:\Users\Brian\Documents\Agentic os\nexus"
git pull origin master
"venv\Scripts\python.exe" "tools\relay_claude_digest.py"
