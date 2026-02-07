@echo off
REM ================================================================================
REM === BALLOM FYR — DASHBOARD LAUNCHER                                          ===
REM ================================================================================
REM Usage:
REM   start_dashboard.bat              → auto-detect mode, port 8050
REM   start_dashboard.bat demo         → force demo mode
REM   start_dashboard.bat live         → force live mode
REM   start_dashboard.bat demo 8060    → demo mode on port 8060
REM ================================================================================
chcp 65001 >nul 2>&1

REM === Force Python to use UTF-8 encoding ===
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

REM === Define Python installation path explicitly ===
set "PYTHON_ROOT=C:\Users\Administrator\AppData\Local\Programs\Python\Python311"
set "PYTHON_EXE=%PYTHON_ROOT%\python.exe"

echo.
echo ================================================================================
echo              BALLOM FYR — DASHBOARD
echo ================================================================================
echo Python path: %PYTHON_EXE%
echo.

REM === Get mode and port arguments ===
set "MODE=%1"
set "PORT=%2"

if "%MODE%"=="" (
    echo [INFO] Mode not specified — dashboard will auto-detect active mode
    echo.
)

if not "%MODE%"=="" (
    echo Mode:     %MODE%
)
if not "%PORT%"=="" (
    echo Port:     %PORT%
) else (
    echo Port:     8050 ^(default^)
)

echo ================================================================================
echo.

REM === Move one directory up ===
cd ..

REM === Check if dash is installed ===
"%PYTHON_EXE%" -c "import dash" >nul 2>&1
if errorlevel 1 (
    echo Installing dashboard dependencies...
    "%PYTHON_EXE%" -m pip install dash plotly
    echo.
)

echo ============= STARTING DASHBOARD =============
echo.
echo   Open your browser to: http://127.0.0.1:%PORT%
echo   Press Ctrl+C to stop the dashboard.
echo.

REM === Launch dashboard with arguments ===
"%PYTHON_EXE%" -X utf8 -u dashboard.py %MODE% %PORT%

echo.
echo ============= DASHBOARD STOPPED =============
pause
