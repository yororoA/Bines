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

REM install.py 将管理器与 QQ 安装到 .venv；兼容旧 thinking_venv。
set "PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=%~dp0thingking\thinking_venv\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo [错误] 未找到 Bines 运行环境:
    echo   %PYTHON%
    echo 请先双击 install.bat 或执行 python install.py。
    pause
    exit /b 1
)

"%PYTHON%" bines.py --web

pause
