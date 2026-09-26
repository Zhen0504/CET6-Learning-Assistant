@echo off
chcp 65001 >nul
title CET-6 写作

cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" (
    echo [错误] 没有找到虚拟环境：
    echo %~dp0.venv
    echo.
    echo 请确认本脚本放在项目根目录。
    pause
    exit /b 1
)

if not exist "写作\app.py" (
    echo [错误] 没有找到 写作\app.py
    echo.
    echo 请确认本脚本放在项目根目录。
    pause
    exit /b 1
)

call ".venv\Scripts\activate.bat"
cd /d "%~dp0写作"

echo ========================================
echo 正在启动 CET-6 写作...
echo 浏览器地址：http://127.0.0.1:5560
echo 停止服务请按 Ctrl+C
echo ========================================
echo.

python app.py

echo.
echo 服务已停止。
pause
