@echo off
chcp 65001 >nul
title 启动本地模型 Ollama
set RUNTIME=%LOCALAPPDATA%\ollama-runtime\ollama.exe
if not exist "%RUNTIME%" (
  echo 未找到本地运行时：%RUNTIME%
  echo 请先运行 scripts\setup_local_model.ps1 安装
  pause
  exit /b 1
)
echo 正在启动 Ollama（本窗口关闭 = 本地模型停止）...
echo 启动后，管理端「模型设置」里会显示已连接。
"%RUNTIME%" serve
pause
