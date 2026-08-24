@echo off
setlocal EnableExtensions

rem Task Scheduler-friendly launcher for pydaq on Windows.
rem Duplicate protection is enforced inside pydaq itself.

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
"%PYTHON_EXE%" -u -m pydaq -c "%CFG_ABS%"
set "PYDAQ_RC=%ERRORLEVEL%"
if not "%PYDAQ_RC%"=="0" call :err "pydaq exited with code %PYDAQ_RC%"
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
