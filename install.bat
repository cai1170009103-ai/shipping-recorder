@echo off
chcp 65001 >nul
title 凯博助手 - 安装
cd /d %~dp0

echo ============================================
echo  凯博助手 v1.2 安装
echo  1) 检查 Python
echo  2) 创建虚拟环境并安装依赖
echo  3) 在桌面创建快捷方式
echo ============================================
echo.

where python >nul 2>nul
if %errorlevel% neq 0 (
  echo [错误] 没找到 Python。
  echo 请先安装 Python 3.10 或更新版：
  echo   https://www.python.org/downloads/
  echo 安装时务必勾选 "Add python.exe to PATH"。
  echo.
  pause
  exit /b 1
)

python --version

if not exist .venv (
  echo 正在创建虚拟环境 ...
  python -m venv .venv
  if %errorlevel% neq 0 (
    echo [错误] 创建虚拟环境失败。
    pause
    exit /b 1
  )
)

echo 正在安装依赖（opencv / numpy / pillow / keyboard）...
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
if %errorlevel% neq 0 (
  echo [警告] 依赖安装可能失败，请把上面的报错发给助手看。
  pause
)

echo 正在创建桌面快捷方式 ...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\凯博助手.lnk');" ^
  "$s.TargetPath='%~dp0run.bat';" ^
  "$s.WorkingDirectory='%~dp0';" ^
  "$s.Description='凯博助手';" ^
  "$s.Save()"

echo.
echo 安装完成！双击桌面的“凯博助手”就能用。
echo 第一次打开：点“设置”选好摄像头、分辨率、保存文件夹。
echo.
pause
