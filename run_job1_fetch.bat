@echo off
set "PROJECT_DIR=%~dp0"
cd /d "%PROJECT_DIR%"
if not exist "%PROJECT_DIR%logs" mkdir "%PROJECT_DIR%logs"
C:\Python312\python.exe "%PROJECT_DIR%agent.py" --fetch --image
if errorlevel 1 exit /b %errorlevel%

rem Wake the on-demand publisher only when a verified story entered either tier.
C:\Python312\python.exe "%PROJECT_DIR%publisher_worker.py" --has-work
if errorlevel 1 exit /b 0

schtasks /Change /TN "GlobalPulseNews_Job2_Publish" /Enable >nul
if errorlevel 1 exit /b %errorlevel%
schtasks /Run /TN "GlobalPulseNews_Job2_Publish" >nul
exit /b %errorlevel%
