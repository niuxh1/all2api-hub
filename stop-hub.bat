@echo off
setlocal EnableExtensions
rem ===========================================================
rem  Hub proxy - stop
rem
rem  Stops ONLY the process recorded in data\hub\hub.pid, and only
rem  after checking that its command line really contains
rem  hub_proxy.py. There is no "taskkill /IM python.exe" style
rem  wildcard matching anywhere in this file: another Python
rem  service on this machine must never be touched.
rem
rem  The hub puts its two upstreams in a job object, so stopping the
rem  hub also stops WorkBuddy and Qoder - even though this script
rem  only kills one PID.
rem
rem  Usage: double-click, or   stop-hub.bat
rem
rem  ASCII-only on purpose (see start-hub.bat). No %VAR% inside an
rem  if( ... ) block.
rem ===========================================================

set "HERE=%~dp0"
set "DATADIR=%HUB_DATA_DIR%"
if not defined DATADIR set "DATADIR=%HERE%data\hub"
set "PIDFILE=%DATADIR%\hub.pid"

echo.
echo Hub - stop
echo.

if not exist "%PIDFILE%" goto no_pid

set "HUBPID="
for /f "usebackq tokens=1" %%P in ("%PIDFILE%") do set "HUBPID=%%P"
if not defined HUBPID goto empty_pid

rem The pid travels to PowerShell through the environment, never
rem through string interpolation, so a garbage pid file cannot
rem inject code.
set "HUB_PID=%HUBPID%"
powershell -NoProfile -Command "$raw = $env:HUB_PID; $id = 0; if (-not [int]::TryParse($raw, [ref]$id)) { exit 3 }; $p = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $id) -ErrorAction SilentlyContinue; if (-not $p) { exit 4 }; if ($p.CommandLine -notlike '*hub_proxy.py*') { exit 5 }; try { Stop-Process -Id $id -Force -ErrorAction Stop } catch { exit 1 }; for ($j = 0; $j -lt 10; $j++) { if (-not (Get-Process -Id $id -ErrorAction SilentlyContinue)) { break }; Start-Sleep -Milliseconds 200 }; if (Get-Process -Id $id -ErrorAction SilentlyContinue) { exit 2 }; exit 0" 2>nul
if errorlevel 9009 goto no_powershell
if errorlevel 5 goto not_hub
if errorlevel 4 goto not_running
if errorlevel 3 goto bad_pid
if errorlevel 2 goto still_alive
if errorlevel 1 goto kill_failed
goto stopped

:stopped
del /f /q "%PIDFILE%" >nul 2>nul
echo Stopped the hub, PID %HUBPID%.
echo PID file removed: %PIDFILE%
goto done_ok

:not_running
del /f /q "%PIDFILE%" >nul 2>nul
echo The process is not running any more, PID %HUBPID%.
echo Removed the stale PID file: %PIDFILE%
goto done_ok

:not_hub
echo [SKIP] PID %HUBPID% does not run hub_proxy.py - not the hub.
echo        Nothing was killed on purpose, so another Python service
echo        is never touched. Check Task Manager and clean it up
echo        manually; the PID file is left in place.
goto done_fail

:bad_pid
echo [SKIP] The PID file does not contain a number: %HUBPID%
echo        File: %PIDFILE%
echo        Check that nothing else writes it, or delete it by hand.
goto done_fail

:empty_pid
echo [SKIP] The PID file is empty: %PIDFILE%
echo        The hub may not have finished starting, or it exited
echo        abnormally. You can delete the file by hand.
goto done_fail

:kill_failed
echo [FAIL] Could not end the process, PID %HUBPID%.
echo        This is usually a permissions problem: run this script
echo        as Administrator.
goto done_fail

:still_alive
echo [FAIL] The stop request was sent but PID %HUBPID% is still alive.
echo        Check Task Manager and end it manually.
goto done_fail

:no_powershell
echo [FAIL] powershell was not found, so the command line could not be
echo        verified and nothing was stopped.
echo        End PID %HUBPID% manually, or delete the PID file and retry.
goto done_fail

:no_pid
echo No PID file found - the hub is probably not running.
echo        Expected: %PIDFILE%
goto done_ok

:done_ok
echo.
pause
exit /b 0

:done_fail
echo.
pause
exit /b 1
