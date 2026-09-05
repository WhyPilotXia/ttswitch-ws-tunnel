@echo off
setlocal
title Start DeepSeek Harness

set "NPM_GLOBAL=%APPDATA%\npm"
set "PATH=%NPM_GLOBAL%;%PATH%"
set "DSH_CMD=dsh"

echo.
echo [DeepSeek Harness]
echo.

where dsh >nul 2>nul
if errorlevel 1 (
    if exist "%NPM_GLOBAL%\dsh.cmd" (
        set "DSH_CMD=%NPM_GLOBAL%\dsh.cmd"
    ) else (
        echo dsh was not found.
        echo Please run 01-Install-DeepSeek-Harness.cmd first.
        echo.
        pause
        exit /b 1
    )
)

"%DSH_CMD%" web

echo.
echo DeepSeek Harness has stopped.
echo.
pause
