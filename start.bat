@echo off
chcp 65001 >nul
cd /d "%~dp0"
title AI 掌柜 · 中小餐饮经营 Agent
echo ============================================
echo   AI 掌柜 · 中小餐饮经营 Agent - 启动中...
echo ============================================
if not exist venv (
    echo [首次运行] 正在创建虚拟环境并安装依赖，请稍候...
    python -m venv venv
    venv\Scripts\pip install -r requirements.txt
)
echo.
echo 提示：首次运行 Windows 防火墙弹窗时，请勾选"专用网络"并点击"允许访问"，
echo       否则手机将无法通过局域网访问点餐端。
echo.
venv\Scripts\python app.py
pause
