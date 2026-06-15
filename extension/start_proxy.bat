@echo off
title JobberAI Proxy (Claude Code)
echo JobberAI proxy — powered by Claude Code CLI
echo No API credits needed. Keep this window open.
echo.
cd /d "%~dp0"
python proxy_server.py
pause
