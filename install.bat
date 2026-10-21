@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
    python install.py %*
) else (
    py -3 install.py %*
)
set "INSTALL_RESULT=%ERRORLEVEL%"
echo.
if not "%INSTALL_RESULT%"=="0" echo 安装未全部完成，请查看上方失败组和日志路径。
pause
exit /b %INSTALL_RESULT%
