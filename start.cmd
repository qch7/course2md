@echo off
rem course2md launcher: forwards all arguments to start.ps1
rem usage: start.cmd -Port 8800 -NoOpen
chcp 65001 >nul
setlocal
cd /d "%~dp0"

where pwsh >nul 2>nul
if %ERRORLEVEL%==0 (
    pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
)

if %ERRORLEVEL% neq 0 (
    echo.
    echo Start failed with exit code %ERRORLEVEL%
    pause
)
endlocal
