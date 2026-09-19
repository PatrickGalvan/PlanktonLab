@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0planktonlab.ps1" %*
if errorlevel 1 (
    echo.
    echo PlanktonLab could not be started. Review the error above.
    pause
    exit /b 1
)
