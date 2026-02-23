@echo off
chcp 65001 >nul
echo ========================================
echo 启动模块管理 Web 界面
echo ========================================
echo.
echo 正在启动 Web 管理服务器...
echo 启动后请访问: http://localhost:5000
echo.
echo 按 Ctrl+C 可以停止服务器
echo ========================================
echo.

cd /d %~dp0
python start_modules.py --web

pause
