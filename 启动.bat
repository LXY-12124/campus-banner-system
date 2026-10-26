@echo off
chcp 65001 >nul
cd /d %~dp0
title 校园条幅报备系统 · 本地服务
echo ====================================================
echo   校园条幅报备系统 · 一键启动
echo ====================================================
echo.
echo   启动后浏览器会自动打开登录页。
echo   公网隧道地址见 tunnel-status.log（每次启动可能变化）。
echo   关闭本窗口即停止服务。
echo.
timeout /t 2 >nul
rem 后台启动公网隧道（Cloudflare Quick Tunnel）
start "" /min "%USERPROFILE%\.cache\cloudflared\cloudflared.exe" tunnel --url http://localhost:8000 --protocol http2 --logfile "%~dp0tunnel-status.log"
start "" http://localhost:8000
python -B server.py
pause
