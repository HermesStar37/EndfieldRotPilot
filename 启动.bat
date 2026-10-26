@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Endfield RotPilot

rem ============================================================
rem Admin check: do NOT use "net session".
rem net session depends on the Windows Server (LanmanServer) service.
rem Optimization tools / antivirus often disable it, which makes the
rem check fail forever -> UAC keeps popping, app never starts.
rem Reading the process token is reliable.
rem ============================================================
powershell -NoProfile -ExecutionPolicy Bypass -Command "$id=[Security.Principal.WindowsIdentity]::GetCurrent();$p=New-Object Security.Principal.WindowsPrincipal($id);if($p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){exit 0}else{exit 1}"
if not errorlevel 1 goto run

rem ---- avoid endless UAC loop ----
rem primary guard: the --elevated argument (always survives Start-Process),
rem secondary: the environment variable
if /i "%~1"=="--elevated" goto elevate_failed
if /i "%ROTPILOT_ELEVATE_TRIED%"=="1" goto elevate_failed
set "ROTPILOT_ELEVATE_TRIED=1"

echo.
echo   Asking for administrator rights, please click "Yes" ...
echo.
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '--elevated' -Verb RunAs"
if errorlevel 1 (
    echo.
    echo   [TIP] Administrator rights were not granted ^(you clicked No^).
    echo         Game keys may not be read. Right-click this file and
    echo         choose "Run as administrator" instead.
    echo.
    pause
)
exit /b

:elevate_failed
echo.
echo   [ERROR] Still not administrator after elevation, stopped to avoid an
echo           endless UAC loop. Right-click this file, then "Run as administrator".
echo.
pause
exit /b 1

:run
if not exist "%~dp0runtime\python.exe" (
    echo   [ERROR] runtime\python.exe not found. This folder is incomplete.
    pause
    exit /b 1
)
start "" /min "%~dp0runtime\python.exe" "%~dp0app_web.py"
exit /b
