@echo off
chcp 65001 >nul
echo ========================================
echo 启动 Bines 并打开管理界面
echo ========================================
echo.
echo 正在自动启动服务与前置依赖...
echo 启动后请访问: http://localhost:5000
echo.
echo 按 Ctrl+C 可以停止服务器
echo ========================================
echo.

cd /d %~dp0

REM Web 管理界面依赖 Flask / flask-cors / pyzmq，这些只装在 thinking_venv 中，
REM 因此必须用 thinking_venv 的解释器启动，不能用系统裸 python。
set "PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=%~dp0thingking\thinking_venv\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo [错误] 未找到 thinking_venv 解释器:
    echo   %PYTHON%
    echo 请检查 thinking_venv 是否完整。
    pause
    exit /b 1
)

"%PYTHON%" bines.py --web

pause
