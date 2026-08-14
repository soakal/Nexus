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

for /f %%i in ('git rev-parse HEAD') do set HEAD_BEFORE=%%i

git pull origin master
"venv\Scripts\python.exe" "tools\relay_claude_digest.py"

REM relay_claude_digest.py can itself merge+pull a pending digest PR
REM (_merge_pending_digest_prs), so HEAD may have moved again after the
REM `git pull` above -- re-check AFTER both, not just after the first pull.
REM NEXUS's own deploy-drift watchdog (backend/agents/watchdog.py) pages
REM daily otherwise: this cron is the one thing that reliably advances the
REM repo's HEAD every day with nobody around to restart the running
REM backend afterward. Restarting here closes that loop at the source --
REM a fresh boot re-captures a matching SHA, so the drift alert never has
REM anything to report.
for /f %%i in ('git rev-parse HEAD') do set HEAD_AFTER=%%i

if not "%HEAD_BEFORE%"=="%HEAD_AFTER%" (
    echo Repo HEAD changed %HEAD_BEFORE% -^> %HEAD_AFTER% -- restarting NEXUS to clear deploy drift
    powershell -NoProfile -ExecutionPolicy Bypass -File "stop.ps1"
    powershell -NoProfile -ExecutionPolicy Bypass -File "start.ps1"
) else (
    echo Repo HEAD unchanged -- no restart needed
)
