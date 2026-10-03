@echo off
rem One-click launcher for the Telegram PDF downloader (GUI).
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.10+ from python.org first.
    pause
    exit /b 1
)

python -c "import telethon" >nul 2>&1
if errorlevel 1 (
    echo First run: installing dependencies, please wait...
    python -m pip install --quiet telethon cryptg || python -m pip install --quiet telethon
)

start "" pythonw pdf_downloader_gui.py
