@echo off
rem NepSync を動かす。タスクスケジューラの「NepSync\run」から呼ばれる（Windows でのつなぎ。IdeaPad の VM に移したら消す。ADR 0013）
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1
cd /d "%~dp0..\work"
if not exist "..\data\logs" mkdir "..\data\logs"
"..\.venv\Scripts\python.exe" -X utf8 -W ignore::ResourceWarning -m nepsync run >> "..\data\logs\run.log" 2>&1
