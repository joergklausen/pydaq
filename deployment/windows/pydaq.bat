@echo off
setlocal EnableExtensions

rem Task Scheduler-friendly launcher for pydaq on Windows.
rem Duplicate protection is enforced inside pydaq itself.
rem Set PYDAQ_DEBUG=1 to capture startup/traceback output and pause on failure.
set "PYDAQ_DEBUG=0"

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%\..\..") do set "REPO_DIR=%%~fI"
set "VENV_DIR=%REPO_DIR%\.venv"
set "PYTHON_EXE=%VENV_DIR%\Scripts\python.exe"
set "ACTIVATE_BAT=%VENV_DIR%\Scripts\activate.bat"

call :parse_args %*
if errorlevel 1 exit /b %errorlevel%

if not exist "%PYTHON_EXE%" (
    call :err "Missing venv python at %PYTHON_EXE%"
    exit /b 1
)

if exist "%ACTIVATE_BAT%" (
    call "%ACTIVATE_BAT%" >nul 2>&1
)

for %%I in ("%CFG_IN%") do set "CFG_TEST=%%~fI"
if exist "%CFG_TEST%" (
    set "CFG_ABS=%CFG_TEST%"
) else (
    for %%I in ("%REPO_DIR%\%CFG_IN%") do set "CFG_ABS=%%~fI"
)

if not exist "%CFG_ABS%" (
    call :err "Config file not found: %CFG_ABS%"
    exit /b 1
)

cd /d "%REPO_DIR%"
call :log "Starting pydaq with config: %CFG_ABS%"

if /I "%PYDAQ_DEBUG%"=="1" goto run_debug

rem Normal mode: preserve live operator display and Task Scheduler exit code.
"%PYTHON_EXE%" -u -m pydaq -c "%CFG_ABS%"
set "PYDAQ_RC=%ERRORLEVEL%"
if not "%PYDAQ_RC%"=="0" call :err "pydaq exited with code %PYDAQ_RC%"
exit /b %PYDAQ_RC%

:run_debug
rem Diagnostic mode: capture all console output to a durable file.
rem Do not log secrets or print the full environment.
set "DEBUG_DIR=%REPO_DIR%\diagnostics"
if not exist "%DEBUG_DIR%" mkdir "%DEBUG_DIR%"
if not exist "%DEBUG_DIR%" (
    call :err "Could not create diagnostic directory: %DEBUG_DIR%"
    exit /b 1
)
set "DEBUG_LOG=%DEBUG_DIR%\pydaq-startup.log"
call :log "Debug capture enabled: %DEBUG_LOG%"
>>"%DEBUG_LOG%" echo ==== pydaq run ====
>>"%DEBUG_LOG%" echo Config: %CFG_ABS%
"%PYTHON_EXE%" -u -m pydaq -c "%CFG_ABS%" >>"%DEBUG_LOG%" 2>&1
set "PYDAQ_RC=%ERRORLEVEL%"
if not "%PYDAQ_RC%"=="0" (
    call :err "pydaq exited with code %PYDAQ_RC%; output saved to %DEBUG_LOG%"
    echo.
    echo --- Recent diagnostic output ---
    powershell -NoProfile -Command "Get-Content -LiteralPath '%DEBUG_LOG%' -Tail 80"
    echo.
    echo Press any key to close this diagnostic console.
    pause >nul
)
exit /b %PYDAQ_RC%

:parse_args
set "CFG_IN="
if "%~1"=="" goto usage_error
if /I "%~1"=="-h" goto usage
if /I "%~1"=="--help" goto usage
if /I "%~1"=="-c" (
    if "%~2"=="" goto usage_error
    set "CFG_IN=%~2"
    exit /b 0
)
if /I "%~1"=="--config" (
    if "%~2"=="" goto usage_error
    set "CFG_IN=%~2"
    exit /b 0
)
set "CFG_IN=%~1"
exit /b 0

:usage
echo Usage:
echo   pydaq.bat CONFIG
echo   pydaq.bat -c CONFIG
echo.
echo CONFIG may be absolute or relative to the repository root.
echo To capture startup errors, run with PYDAQ_DEBUG=1.
exit /b 0

:usage_error
call :err "A station configuration is required."
call :usage
exit /b 2

:timestamp
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-ddTHH:mm:ss"') do set "TS=%%I"
exit /b 0

:log
call :timestamp
echo %TS%, INFO, pydaq.bat, %~1
exit /b 0

:err
call :timestamp
>&2 echo %TS%, ERROR, pydaq.bat, %~1
exit /b 0
