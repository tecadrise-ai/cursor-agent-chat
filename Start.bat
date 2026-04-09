@echo off
cd /d "%~dp0"
set PYTHONDONTWRITEBYTECODE=1
echo Starting Agent Chat CLI test server...
echo Default URL: http://127.0.0.1:8765  (edit config.toml for port/host)
echo Press Ctrl+C to stop.
python server.py
pause
