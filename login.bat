@echo off
rem One-click local login: find a logged-in Telegram Desktop / AyuGram tdata on this
rem machine and convert it into the downloader session (no phone code needed).
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.10+ from python.org first.
    pause
    exit /b 1
)

python -c "import telethon, opentele" >nul 2>&1
if errorlevel 1 (
    echo First run: installing dependencies, please wait...
    python -m pip install --quiet telethon cryptg opentele || python -m pip install --quiet telethon
)

python convert_tdata.py %*
echo.
pause
