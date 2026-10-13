@echo off
REM TrustPay - one-click local server (Windows)
REM First run: creates .venv and installs Flask. Later runs start immediately.
cd /d "%~dp0"
title TrustPay local server

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo [ERROR] Python was not found.
    echo Install Python 3.10+ from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment in .venv ...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Could not create the virtual environment.
        pause
        exit /b 1
    )
)

echo Installing / checking dependencies ...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
    echo [ERROR] pip install failed - check your internet connection.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   TrustPay is running at  http://127.0.0.1:5000
".venv\Scripts\python.exe" -c "import config; print('  Contract: Sepolia ' + (config.CONTRACT_ADDRESS or '(not configured - see config.py)'))"
echo   Close this window or press Ctrl+C to stop the server.
echo ============================================================
echo.

REM open the browser a few seconds after the server starts
start "" cmd /c "timeout /t 4 /nobreak >nul & start http://127.0.0.1:5000"

".venv\Scripts\python.exe" app.py
pause
