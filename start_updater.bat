@echo off
REM ================================================================================
REM === UPDATER LAUNCHER — NIFTY ONLY (dev_update_nifty)
REM ================================================================================
REM Usage:  start_updater.bat [demo|live]
REM
REM Reads Fyers token from C:/Ballom_FYR/fyers_token.json (written by dev_scanner).
REM Continuously computes SHA signals for NIFTY only and writes
REM signal_state.json for the dev branch dashboard.
REM Sibling branches: dev_update_silver (SILVERM), dev_update_gold (GOLDM)
REM
REM Auto-restarts on crash via forever loop.
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
echo              FYERS SHA UPDATER LAUNCHER [NIFTY]
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

REM === Mode info ===
if /i "%MODE%"=="live" goto :LIVE_WARNING
goto :DEMO_INFO

:LIVE_WARNING
echo.
echo ********************************************************************************
echo                           LIVE MODE ACTIVE
echo ********************************************************************************
echo.
echo    LIVE data — reads live Fyers token, fetches real market data.
echo    This service does NOT place any orders.
echo.
echo ********************************************************************************
echo.
goto :CONTINUE_SCRIPT

:DEMO_INFO
echo.
echo [DEMO MODE] Uses demo Fyers token — no real-money risk
echo.

:CONTINUE_SCRIPT

echo ============= STARTING UPDATER =============

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
echo Launching updater in %MODE% mode
echo ================================================================================
echo.

REM === Forever-restart loop — auto-recover from crashes ===
:FOREVER
"%PYTHON_EXE%" -X utf8 -u updater_app.py %MODE%
echo.
echo [WARNING] Updater exited unexpectedly — restarting in 10 seconds...
echo           Press Ctrl+C to abort.
timeout /t 10 /nobreak >nul
goto :FOREVER
