@echo off
set "PROJECT_DIR=%~dp0"
cd /d "%PROJECT_DIR%"
if not exist "%PROJECT_DIR%logs" mkdir "%PROJECT_DIR%logs"

rem FIX 3: Wait for network before doing anything - retries for up to 3 minutes
set NETWORK_READY=0
for /L %%i in (1,1,18) do (
    ping -n 1 -w 1000 8.8.8.8 >nul 2>&1
    if not errorlevel 1 (
        set NETWORK_READY=1
        goto :network_ok
    )
    timeout /t 10 /nobreak >nul
)
:network_ok
if "%NETWORK_READY%"=="0" (
    echo %DATE% %TIME% WARNING: No network after 3 min wait - skipping publish run >> "%PROJECT_DIR%logs\job2_errors.log"
    exit /b 0
)

rem Check if this job triggered on time - logs warning to logs/ if late
C:\Python312\python.exe "%PROJECT_DIR%pipeline\schedule_monitor.py" --job publish

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
