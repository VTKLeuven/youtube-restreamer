@echo off
rem Double-click me. Asks for the YouTube URL, then shares it with VLC on the LAN.
rem Extra arguments are passed through, e.g.:  start-windows.bat <url> --quality 720p
cd /d "%~dp0"

where uv >nul 2>nul
if %errorlevel%==0 (
    uv run restream.py %*
    goto end
)

if not exist ".venv\Scripts\python.exe" (
    echo First run: setting up, this takes a minute...
    py -3 -m venv .venv 2>nul || python -m venv .venv
    if not exist ".venv\Scripts\python.exe" (
        echo.
        echo Python 3 was not found. Install it from https://www.python.org/downloads/
        echo and tick "Add python.exe to PATH" during setup, then run this again.
        goto end
    )
    ".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check --upgrade streamlink
)
".venv\Scripts\python.exe" restream.py %*

:end
pause
