@echo off
setlocal
title Install DeepSeek Harness

pushd "%~dp0" >nul

set "PORT=3080"
set "BASE=http://127.0.0.1:%PORT%"
set "INJECT=TT-Switch-Models.ps1"
set "NPM_GLOBAL=%APPDATA%\npm"
set "PATH=%NPM_GLOBAL%;%PATH%"

echo.
echo [DeepSeek Harness install]
echo First install can take several minutes. Many npm verbose lines are normal.
echo Keep this window open until it says Done.
echo.

where npm >nul 2>nul
if errorlevel 1 (
    echo Node.js/npm was not found.
    echo Please install Node.js LTS first, then run this script again.
    echo https://nodejs.org/
    echo.
    pause
    popd >nul
    exit /b 1
)

echo Setting npm registry mirror...
call npm config set registry https://registry.npmmirror.com
if errorlevel 1 goto fail

echo.
echo Installing @deepseek-ai/dsh...
call npm install -g @deepseek-ai/dsh --verbose
if errorlevel 1 goto fail

call :FindDsh
if errorlevel 1 goto fail

echo.
echo Checking dsh...
call "%DSH_CMD%" --version
if errorlevel 1 goto fail

if not exist "%INJECT%" (
    echo.
    echo Model setup script was not found:
    echo %CD%\%INJECT%
    echo Keep this file next to the install script.
    echo.
    pause
    popd >nul
    exit /b 1
)

echo.
echo Starting DeepSeek Harness Web for first-time model setup...
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 '%BASE%'; if ($r.StatusCode -ge 200) { exit 0 } } catch {}; exit 1"
if errorlevel 1 (
    start "DeepSeek Harness Web" cmd /k ""%DSH_CMD%" web --no-open --port %PORT%"
) else (
    echo DeepSeek Harness Web is already running.
)

echo Waiting for Web service...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$deadline = (Get-Date).AddSeconds(90); do { try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 '%BASE%'; if ($r.StatusCode -ge 200) { exit 0 } } catch {}; Start-Sleep -Seconds 2 } until ((Get-Date) -gt $deadline); exit 1"
if errorlevel 1 (
    echo.
    echo Web service did not start within 90 seconds.
    echo Check the separate DeepSeek Harness Web window for errors.
    echo.
    pause
    popd >nul
    exit /b 1
)

echo.
echo Setting up TT Switch model list...
powershell -NoProfile -ExecutionPolicy Bypass -File ".\%INJECT%" -Port %PORT%
if errorlevel 1 (
    echo.
    echo Model setup failed. Send a screenshot of this window to the administrator.
    echo.
    pause
    popd >nul
    exit /b 1
)

echo.
echo Done. DeepSeek Harness is installed and TT Switch models are set up.
echo Opening browser...
start "" "%BASE%"
echo.
echo Next time, run: 02-Start-DeepSeek-Harness.cmd
echo.
pause
popd >nul
exit /b 0

:FindDsh
set "DSH_CMD=dsh"
where dsh >nul 2>nul
if not errorlevel 1 exit /b 0
if exist "%NPM_GLOBAL%\dsh.cmd" (
    set "DSH_CMD=%NPM_GLOBAL%\dsh.cmd"
    exit /b 0
)
echo dsh was installed, but the command was not found in PATH.
echo Try closing this window and running the start script again.
exit /b 1

:fail
echo.
echo Install failed. Send a screenshot of this window to the administrator.
echo.
pause
popd >nul
exit /b 1
