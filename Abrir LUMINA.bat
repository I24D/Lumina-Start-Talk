@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo LUMINA cannot start: its Python environment .venv is missing.
    echo Create it from this folder with:
    echo     py -3 -m venv .venv
    echo     .venv\Scripts\python.exe setup.py
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "main.py"
