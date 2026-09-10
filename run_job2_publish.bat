@echo off
set "PROJECT_DIR=%~dp0"
cd /d "%PROJECT_DIR%"
if not exist "%PROJECT_DIR%logs" mkdir "%PROJECT_DIR%logs"
C:\Python312\python.exe "%PROJECT_DIR%publisher_worker.py" --has-work
if errorlevel 1 (
    schtasks /Change /TN "GlobalPulseNews_Job2_Publish" /Disable >nul
    exit /b 0
)

C:\Python312\python.exe "%PROJECT_DIR%publisher_worker.py"
set "PUBLISH_RESULT=%errorlevel%"

rem Stay enabled after an error only when verified work still needs recovery.
C:\Python312\python.exe "%PROJECT_DIR%publisher_worker.py" --has-work
if errorlevel 1 schtasks /Change /TN "GlobalPulseNews_Job2_Publish" /Disable >nul
exit /b %PUBLISH_RESULT%
