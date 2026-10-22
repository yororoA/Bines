@echo off
setlocal
chcp 437 >nul 2>&1
title Docker fix step 2 - reinstall Docker Desktop

net session >nul 2>&1
if not "%errorlevel%"=="0" goto need_admin

echo ==========================================================
echo  Step 2: repair leftover permissions and reinstall Docker
echo  Run this AFTER the reboot from step 1.
echo ==========================================================
echo.

echo [0/4] Sanity check: WSL platform service ...
sc query vmcompute | findstr /i "RUNNING STOPPED" >nul 2>&1
if errorlevel 1 (
    echo       WARNING: vmcompute service not found.
    echo       Step 1 did not take effect, or the reboot is missing.
    echo       Continuing anyway in 8 seconds - press Ctrl+C to abort.
    timeout /t 8 >nul
) else (
    echo       ok
)
echo.

echo [1/4] Fix ownership of C:\ProgramData\DockerDesktop ...
if exist "C:\ProgramData\DockerDesktop" (
    takeown /f "C:\ProgramData\DockerDesktop" /r /d y >nul 2>&1
    icacls "C:\ProgramData\DockerDesktop" /grant *S-1-5-32-544:F /t >nul 2>&1
    echo       ok
) else (
    echo       not present, skipped
)
echo.

echo [2/4] Removing leftovers from the failed install ...
if exist "C:\Program Files\Docker" (
    rd /s /q "C:\Program Files\Docker" >nul 2>&1
    if exist "C:\Program Files\Docker" (
        echo       WARNING: could not fully remove C:\Program Files\Docker
    ) else (
        echo       C:\Program Files\Docker removed
    )
) else (
    echo       C:\Program Files\Docker not present
)
if exist "%LOCALAPPDATA%\Docker" rd /s /q "%LOCALAPPDATA%\Docker" >nul 2>&1
if exist "%APPDATA%\Docker" rd /s /q "%APPDATA%\Docker" >nul 2>&1
echo.

echo [3/4] Installing Docker Desktop via winget (about 600 MB) ...
echo       This takes several minutes. Do not close the window.
winget install --id Docker.DockerDesktop --exact --accept-source-agreements --accept-package-agreements --disable-interactivity
echo       exit code: %errorlevel%
echo.

echo [4/4] Starting Docker Desktop ...
if exist "C:\Program Files\Docker\Docker\Docker Desktop.exe" (
    start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    echo       launched
) else (
    echo       Docker Desktop.exe not found - installation may have failed
)
echo.

echo ==========================================================
echo  Finish in the Docker Desktop window:
echo    1. Accept the subscription service agreement
echo    2. Skip or finish the sign-in prompt
echo    3. Wait until the whale icon stops animating
echo  Then verify in a new terminal:  docker version
echo ==========================================================
echo.
pause
exit /b 0

:need_admin
echo [ERROR] This script needs administrator rights.
echo         Right-click it and choose "Run as administrator".
echo.
pause
exit /b 1
