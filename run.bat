@echo off
chcp 65001 >nul
title 数据抢救工作台
rem ---- 检查管理员权限, 不够则提权重启 ----
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo 需要管理员权限读取物理盘, 正在请求提权...
  powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%~f0' -WorkingDirectory '%~dp0'"
  exit /b
)
cd /d "%~dp0"
rem ---- 选一个空闲端口 ----
set PORT=8765
rem ---- 启动服务并打开浏览器 ----
start "数据抢救工作台服务" /min python server.py %PORT%
timeout /t 2 /nobreak >nul
start http://127.0.0.1:%PORT%
echo.
echo  数据抢救工作台已启动: http://127.0.0.1:%PORT%
echo  关闭此窗口或最小化窗口内的 python 即可停止服务.
pause >nul
