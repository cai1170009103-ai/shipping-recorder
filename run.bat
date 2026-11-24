@echo off
chcp 65001 >nul
cd /d %~dp0
if exist .venv\Scripts\pythonw.exe (
  start "" .venv\Scripts\pythonw.exe shiprecorder.py
) else if exist .venv\Scripts\python.exe (
  start "" .venv\Scripts\python.exe shiprecorder.py
) else (
  echo 没找到虚拟环境，请先双击 install.bat 安装。
  pause
)
