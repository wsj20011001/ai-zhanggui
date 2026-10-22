@echo off
chcp 65001 >nul
title AI 掌柜 · 演示启动
cd /d "%~dp0"

set RUNTIME=%LOCALAPPDATA%\ollama-runtime\ollama.exe

echo [1/3] 本地模型
if exist "%RUNTIME%" (
  start "Ollama 本地模型" /min "%RUNTIME%" serve
  echo       已启动（窗口最小化，关掉即停）
) else (
  echo       未安装，跳过 —— 云端通道照常可用
)

echo [2/3] 应用服务
start "AI 掌柜 服务" /min venv\Scripts\python.exe app.py --no-browser

echo [3/3] 等待就绪并打开浏览器
timeout /t 7 >nul
start "" http://127.0.0.1:5100/admin

echo.
echo 演示已就绪：http://127.0.0.1:5100/admin   账号 admin / admin123
echo 如果页面打不开，看任务栏里两个最小化的窗口有没有报错。
echo.
pause
