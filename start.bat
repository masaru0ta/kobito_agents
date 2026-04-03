@echo off
chcp 65001 >nul 2>&1

set PORT=8200

netstat -ano | findstr "LISTENING" | findstr ":%PORT% " >nul 2>&1
if not errorlevel 1 (
    echo Server already running on port %PORT%
    start http://localhost:%PORT%
    exit /b
)

echo Starting kobito_agents on port %PORT%...
cd /d "%~dp0src"

set FIRST=1

:loop
if "%FIRST%"=="1" (
    set FIRST=0
    start "" cmd /c "ping -n 5 127.0.0.1 >nul 2>&1 && start http://localhost:%PORT%"
)
echo [%date% %time%] Server starting...
uvicorn server.app:app --host 0.0.0.0 --port %PORT%
echo [%date% %time%] Server exited. Restarting in 2 seconds...
ping -n 3 127.0.0.1 >nul 2>&1
goto loop
