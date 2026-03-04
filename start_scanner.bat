@echo off
REM ================================================================================
REM === SCANNER LAUNCHER — Hourly option-pair scanning service
REM ================================================================================
REM Usage:  start_scanner.bat [demo|live]
REM
REM Starts the scanner in a forever-restart loop so it automatically
REM recovers from crashes.  Token is persisted at C:/Ballom_FYR/fyers_token.json
REM and is reusable by any other branch (dev, dev_trader, etc.).
REM ================================================================================

REM === Jenkins Unicode fix ===
chcp 65001 >nul 2>&1

REM === Force Python to use UTF-8 encoding ===
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

REM === Python path ===
set "PYTHON_ROOT=C:\Users\Administrator\AppData\Local\Programs\Python\Python311"
set "PYTHON_EXE=%PYTHON_ROOT%\python.exe"

echo.
echo ================================================================================
echo              FYERS SCANNER LAUNCHER
echo ================================================================================
echo Python path: %PYTHON_EXE%
echo.

REM === Get mode argument ===
set "MODE=%1"
if "%MODE%"=="" (
    set "MODE=demo"
    echo [INFO] Mode not specified, defaulting to DEMO mode for safety
)

echo.
echo ================================================================================
echo Mode:     %MODE%
echo ================================================================================

REM === Safety warning for LIVE mode ===
if /i "%MODE%"=="live" goto :LIVE_WARNING
goto :DEMO_INFO

:LIVE_WARNING
echo.
echo ********************************************************************************
echo                           LIVE MODE ACTIVE
echo ********************************************************************************
echo.
echo    LIVE auth — token will be written for real account!
echo    Scanner does NOT place orders, but other branches may use
echo    the token file for live trading.
echo.
echo ********************************************************************************
echo.
goto :CONTINUE_SCRIPT

:DEMO_INFO
echo.
echo [DEMO MODE] Scanner uses demo auth — no real-money risk
echo.

:CONTINUE_SCRIPT

echo ============= STARTING SCANNER =============

REM === Install dependencies on first run ===
if not exist ".\deps_installed.flag" goto :INSTALL_DEPS
goto :SKIP_DEPS

:INSTALL_DEPS
echo Installing dependencies...
"%PYTHON_EXE%" -m ensurepip --upgrade
"%PYTHON_EXE%" -m pip install --upgrade pip setuptools wheel
"%PYTHON_EXE%" -m pip install -r requirements_fyers.txt
echo Dependencies installed > ".\deps_installed.flag"
goto :RUN_SCRIPT

:SKIP_DEPS
echo Dependencies already installed, skipping...

:RUN_SCRIPT
echo.
echo ================================================================================
echo Launching scanner in %MODE% mode
echo ================================================================================
echo.

REM === Forever-restart loop — auto-recover from crashes ===
:FOREVER
"%PYTHON_EXE%" -X utf8 -u scanner_app.py %MODE%
echo.
echo [WARNING] Scanner exited unexpectedly — restarting in 10 seconds...
echo           Press Ctrl+C to abort.
timeout /t 10 /nobreak >nul
goto :FOREVER
