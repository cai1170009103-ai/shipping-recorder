@echo off
chcp 65001 >nul
title 发货录制终端 - 打包 exe
cd /d %~dp0

where python >nul 2>nul
if %errorlevel% neq 0 (
  echo [错误] 没找到 Python，请先装 Python 3.10+ 并勾选 Add to PATH。
  pause
  exit /b 1
)
if not exist .venv (
  echo 请先双击 install.bat 安装依赖。
  pause
  exit /b 1
)

echo 正在安装 PyInstaller ...
.venv\Scripts\python -m pip install pyinstaller
echo 正在打包（单文件 exe，约 1-2 分钟）...
.venv\Scripts\python -m PyInstaller --noconfirm --onefile --windowed ^
  --name "发货录制终端" --icon NONE shiprecorder.py
if %errorlevel% neq 0 (
  echo [错误] 打包失败。
  pause
  exit /b 1
)

echo.
echo 打包完成：dist\发货录制终端.exe
echo 把这个 exe 拷到任何一台 Windows 电脑上都能直接双击运行（不用装 Python）。
echo 注意：exe 第一次启动会在同目录生成 config.json / count.json / records.jsonl，
echo 视频默认存在 exe 旁边 videos 文件夹，可在设置里改。
echo.
pause
