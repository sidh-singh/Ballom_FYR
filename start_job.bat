@echo off
REM ================================================================================
REM === JENKINS UNICODE FIX: Set console to UTF-8 encoding ===
REM ================================================================================
REM This ensures Unicode characters (box-drawing, special symbols) display correctly
REM in Jenkins console output on Windows. Remove this if not needed.
chcp 65001 >nul 2>&1

REM === Force Python to use UTF-8 encoding (CRITICAL for Unicode support) ===
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

REM === Define Python installation path explicitly ===
set "PYTHON_ROOT=C:\Users\Administrator\AppData\Local\Programs\Python\Python311"
set "PYTHON_EXE=%PYTHON_ROOT%\python.exe"

echo.
echo ================================================================================
echo              FYERS JOB LAUNCHER
echo ================================================================================
echo Python path: %PYTHON_EXE%
echo.

REM === Get mode argument (strategy parameter removed) ===
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
echo                            LIVE MODE ACTIVE
echo ********************************************************************************
echo.
echo    LIVE TRADING WITH REAL MONEY - Orders will be placed immediately!
echo.
echo ********************************************************************************
echo.
goto :CONTINUE_SCRIPT

:DEMO_INFO
echo.
echo [DEMO MODE] Paper trading - No real money at risk
echo [DEMO MODE] Data stored at C:\AlgoTrading_Demo\
echo.

:CONTINUE_SCRIPT

REM === Move one directory up ===
cd ..

echo ============= STARTING SCRIPT =============

REM === Check if requirements already installed ===
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
echo Launching job in %MODE% mode
echo ================================================================================
echo.

REM === Run launcher with mode only ===
"%PYTHON_EXE%" -X utf8 -u app.py %MODE%

echo.
echo ============= SCRIPT FINISHED =============
pause

