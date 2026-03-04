@echo off
REM ════════════════════════════════════════════════════════════════════════════
REM  start_trading.bat — Launch the Ballom FYR Trading Engine
REM
REM  Usage:
REM    start_trading.bat          → demo mode (default)
REM    start_trading.bat demo     → paper trading (DemoFyers)
REM    start_trading.bat live     → real trading (Fyers)
REM
REM  Prerequisites:
REM    - dev_scanner must be running (writes fyers_token.json + pair JSONs)
REM    - dev_update should be running (writes signal_state.json)
REM    - Python 3.11+ on PATH (or edit PYTHON_EXE below)
REM
REM  This script auto-restarts on crash with an increasing backoff
REM  (5s → 10s → 20s → … max 120s).  It logs crash timestamps to
REM  C:\Ballom_FYR\logs\trading_crashes.log.
REM ════════════════════════════════════════════════════════════════════════════

setlocal enabledelayedexpansion

REM ── Configuration ────────────────────────────────────────────────────────────
set "MODE=%~1"
if "%MODE%"=="" set "MODE=demo"

set "PYTHON_EXE=C:\Users\Administrator\AppData\Local\Programs\Python\Python311\python.exe"
set "SCRIPT_DIR=%~dp0"
set "SCRIPT=trading_app.py"
set "LOG_DIR=C:\Ballom_FYR\logs"
set "CRASH_LOG=%LOG_DIR%\trading_crashes.log"

REM ── Ensure log directory exists ──────────────────────────────────────────────
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM ── Title ────────────────────────────────────────────────────────────────────
title Ballom FYR Trading Engine [%MODE%]

echo.
echo  ╔══════════════════════════════════════════════════════════════╗
echo  ║           Ballom FYR — Trading Engine                       ║
echo  ║                                                              ║
echo  ║  Mode : %MODE%                                               ║
echo  ║  Script : %SCRIPT%                                          ║
echo  ╚══════════════════════════════════════════════════════════════╝
echo.

REM ── Check Python ─────────────────────────────────────────────────────────────
%PYTHON_EXE% --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found on PATH. Install Python 3.11+ or edit PYTHON_EXE.
    pause
    exit /b 1
)

REM ── Check token file exists ──────────────────────────────────────────────────
if not exist "C:\Ballom_FYR\fyers_token.json" (
    echo [WARNING] fyers_token.json not found — ensure dev_scanner is running.
    echo           Trading engine will retry loading token on startup...
    echo.
)

REM ── Backoff variables ────────────────────────────────────────────────────────
set "BACKOFF=5"
set "MAX_BACKOFF=120"
set "RUN_COUNT=0"

REM ── Auto-restart loop ────────────────────────────────────────────────────────
:LOOP
set /a RUN_COUNT+=1
echo [%date% %time%] Starting run #%RUN_COUNT% in %MODE% mode...
echo [%date% %time%] Run #%RUN_COUNT% started >> "%CRASH_LOG%"

cd /d "%SCRIPT_DIR%"
%PYTHON_EXE% %SCRIPT% %MODE%

set "EXIT_CODE=%errorlevel%"
echo.
echo [%date% %time%] Process exited with code %EXIT_CODE%
echo [%date% %time%] Exit code: %EXIT_CODE% >> "%CRASH_LOG%"

if %EXIT_CODE%==0 (
    echo [INFO] Clean exit — not restarting.
    goto :END
)

echo [WARNING] Crash detected — restarting in %BACKOFF% seconds...
echo [%date% %time%] Crash — restarting in %BACKOFF%s >> "%CRASH_LOG%"

timeout /t %BACKOFF% /nobreak >nul

REM ── Increase backoff (exponential, capped) ──────────────────────────────────
set /a BACKOFF=%BACKOFF%*2
if %BACKOFF% gtr %MAX_BACKOFF% set "BACKOFF=%MAX_BACKOFF%"

goto :LOOP

:END
echo.
echo  Trading engine stopped.
pause
