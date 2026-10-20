@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 打包 春归燕茶餐厅
echo 正在安装 PyInstaller 并打包（首次耗时较长）...
venv\Scripts\pip install pyinstaller
venv\Scripts\pyinstaller --noconfirm build.spec
echo.
echo 打包完成！程序位置：dist\春归燕茶餐厅\春归燕茶餐厅.exe
echo 将该文件夹整体复制到其他电脑即可双击运行（无需安装 Python）。
pause
