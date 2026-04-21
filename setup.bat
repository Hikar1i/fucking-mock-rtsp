@echo off
setlocal

echo === Local Mock RTSP Setup ===
echo.

REM Check Python
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found in PATH.
    echo Please install Python 3.11+ from https://python.org
    pause & exit /b 1
)

REM Check FFmpeg
where ffmpeg >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] FFmpeg not found in PATH.
    echo Please install FFmpeg: https://ffmpeg.org/download.html
    pause & exit /b 1
)

REM Install uv if missing
where uv >nul 2>&1
if %errorlevel% neq 0 (
    echo [INFO] Installing uv...
    pip install uv
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to install uv via pip.
        echo Try: powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
        pause & exit /b 1
    )
)

echo [INFO] Creating virtual environment and installing dependencies...
uv sync
if %errorlevel% neq 0 (
    echo [ERROR] uv sync failed.
    pause & exit /b 1
)

echo.
echo [OK] Setup complete!
echo Run 'run.bat' to start the server.
pause
