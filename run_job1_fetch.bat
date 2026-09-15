@echo off
set "PROJECT_DIR=%~dp0"
cd /d "%PROJECT_DIR%"
if not exist "%PROJECT_DIR%logs" mkdir "%PROJECT_DIR%logs"

rem Check if this job triggered on time - logs warning to logs/ if late
C:\Python312\python.exe "%PROJECT_DIR%pipeline\schedule_monitor.py" --job fetch

C:\Python312\python.exe "%PROJECT_DIR%agent.py" --fetch --image
if errorlevel 1 exit /b %errorlevel%

rem Wake the on-demand publisher only when a verified story entered either tier.
C:\Python312\python.exe "%PROJECT_DIR%publisher_worker.py" --has-work
if errorlevel 1 exit /b 0

schtasks /Change /TN "GlobalPulseNews_Job2_Publish" /Enable >nul
if errorlevel 1 exit /b %errorlevel%
schtasks /Run /TN "GlobalPulseNews_Job2_Publish" >nul
exit /b %errorlevel%
