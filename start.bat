@echo off
chcp 65001 >nul 2>&1

set PORT=8300

netstat -ano | findstr "LISTENING" | findstr ":%PORT% " >nul 2>&1
if not errorlevel 1 (
    echo Server already running on port %PORT%
    start http://localhost:%PORT%
    exit /b
)

echo Starting kobito_agents on port %PORT%...
cd /d "%~dp0src"
start "kobito_agents server" uvicorn server.app:app --host 127.0.0.1 --port %PORT% --reload --reload-dir server

set count=0
:wait
ping -n 2 127.0.0.1 >nul 2>&1
curl -s --connect-timeout 1 http://localhost:%PORT%/api/agents >nul 2>&1
if not errorlevel 1 goto ready
set /a count=count+1
if %count% GEQ 30 (
    echo Error: Server startup timed out
    exit /b 1
)
goto wait

:ready
echo Server ready
start http://localhost:%PORT%
