@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if exist "runtime\python\python.exe" (
    "runtime\python\python.exe" -B install_runtime.py %*
    if errorlevel 1 pause
    exit /b
)
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m film_worker launch %*
) else (
    where uv.exe >nul 2>&1
    if errorlevel 1 (
        echo 请先安装 uv，再重新打开执行端。
        pause
        exit /b 1
    )
    uv run --locked python -m film_worker launch %*
)
if errorlevel 1 pause
