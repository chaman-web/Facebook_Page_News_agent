@echo off
set "PROJECT_DIR=%~dp0"
cd /d "%PROJECT_DIR%"
if not exist "%PROJECT_DIR%logs" mkdir "%PROJECT_DIR%logs"
C:\Python312\python.exe "%PROJECT_DIR%agent.py" --publish
