@echo off
setlocal
chcp 437 >nul 2>&1
title Docker fix step 1 - enable WSL2 platform

net session >nul 2>&1
if not "%errorlevel%"=="0" goto need_admin

echo ==========================================================
echo  Step 1: enable the virtualization features Docker needs
echo ==========================================================
echo.

echo [1/3] VirtualMachinePlatform ...
dism /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart
echo       exit code: %errorlevel%
echo.

echo [2/3] Microsoft-Windows-Subsystem-Linux ...
dism /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart
echo       exit code: %errorlevel%
echo.

echo [3/3] Set WSL default version to 2 ...
wsl --set-default-version 2
echo       exit code: %errorlevel%
echo.

echo ==========================================================
echo  Done. A REBOOT is required before step 2.
echo  After rebooting, run docker-fix-2-install.bat as admin.
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
