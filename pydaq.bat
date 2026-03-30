@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem pydaq.bat — Task Scheduler-friendly launcher for pydaq on Windows
rem - prefers the local .venv
rem - prevents double-starts per config using a PowerShell process query
rem - runs: python -m pydaq -c <config>

set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "REPO_DIR=%SCRIPT_DIR%"
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
    call :log ".venv activated (%VENV_DIR%)"
) else (
    call :log "No activate.bat found, using %PYTHON_EXE% directly"
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

set "CFG_REL="
call set "REPO_PREFIX=%%REPO_DIR%%\"
call set "CFG_TMP=%%CFG_ABS:%REPO_PREFIX%=%%"
if /I not "%CFG_TMP%"=="%CFG_ABS%" set "CFG_REL=%CFG_TMP%"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$cfgAbs = [Regex]::Escape($env:CFG_ABS);" ^
  "$cfgRel = [Regex]::Escape($env:CFG_REL);" ^
  "$procs = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and ($_.Name -match 'python(\.exe)?$' -or $_.Name -match 'py(\.exe)?$') };" ^
  "$matchAbs = $procs | Where-Object { $_.CommandLine -match ('-m\s+pydaq.*-c\s+"?' + $cfgAbs + '"?') };" ^
  "$matchRel = @();" ^
  "if ($env:CFG_REL) { $matchRel = $procs | Where-Object { $_.CommandLine -match ('-m\s+pydaq.*-c\s+"?' + $cfgRel + '"?') } }" ^
  "if (($matchAbs | Measure-Object).Count -gt 0 -or ($matchRel | Measure-Object).Count -gt 0) { exit 10 } else { exit 0 }"
if "%ERRORLEVEL%"=="10" (
    call :log "Already running for config: %CFG_ABS%"
    exit /b 0
)
if errorlevel 1 (
    call :err "Process query failed. Refusing to start a duplicate-unsafely."
    exit /b 1
)

cd /d "%REPO_DIR%"
call :log "Starting pydaq with config: %CFG_ABS%"
"%PYTHON_EXE%" -u -m pydaq -c "%CFG_ABS%"
exit /b %ERRORLEVEL%

:parse_args
set "CFG_IN="
if "%~1"=="" (
    set "CFG_IN=pydaq\configs\buc.yml"
    exit /b 0
)
if /I "%~1"=="-h" goto usage
if /I "%~1"=="--help" goto usage
if /I "%~1"=="-c" (
    if "%~2"=="" (
        call :err "Missing CONFIG after -c/--config"
        goto usage_error
    )
    set "CFG_IN=%~2"
    exit /b 0
)
if /I "%~1"=="--config" (
    if "%~2"=="" (
        call :err "Missing CONFIG after -c/--config"
        goto usage_error
    )
    set "CFG_IN=%~2"
    exit /b 0
)
set "CFG_IN=%~1"
exit /b 0

:usage
@echo Usage:
@echo   pydaq.bat [CONFIG]
@echo   pydaq.bat -c CONFIG
@echo.
@echo Examples:
@echo   pydaq.bat pydaq\configs\buc.yml
@echo   pydaq.bat -c pydaq\configs\other_site.yml
@echo.
@echo Notes:
@echo   - CONFIG may be absolute or relative to the repo root.
@echo   - One running instance per CONFIG is allowed.
exit /b 0

:usage_error
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
