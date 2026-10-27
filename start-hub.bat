@echo off
setlocal EnableExtensions
rem ===========================================================
rem  Fusion hub (WorkBuddy + Qoder + ZCode) - launcher
rem  Usage: double-click, or  start-hub.bat [port]
rem
rem  ASCII-only on purpose: .bat files are read using the
rem  console code page, so non-ASCII text breaks the parser.
rem
rem  NOTE: this script puts no %VAR% inside an if( ... ) block,
rem  because a path containing ( or ) breaks the cmd.exe parser.
rem  All checks use if/errorlevel/goto instead.
rem
rem  SILENT MODE: with a GUI-subsystem pythonw the hub runs with
rem  NO console window and nothing in the taskbar; closing any
rem  window never stops it. A separate live log viewer opens on
rem  top (console_tail.py --file) - closing the viewer only
rem  closes the viewer. When no pythonw exists we fall back to
rem  the visible-window launcher.
rem ===========================================================

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8787"
set "HERE=%~dp0"
set "SCRIPT=%HERE%hub_proxy.py"
set "DATADIR=%HUB_DATA_DIR%"
if not defined DATADIR set "DATADIR=%HERE%data\hub"
set "LOGDIR=%DATADIR%\logs"
set "LOGFILE=%LOGDIR%\hub.log"
set "PIDFILE=%DATADIR%\hub.pid"
set "PYARGS=--port %PORT% --pid-file "%PIDFILE%""
if /i "%HUB_LAUNCH_LAN%"=="1" set "PYARGS=%PYARGS% --lan"

if not exist "%SCRIPT%" goto no_script

rem ---- explicit visible-console mode ----
rem The -visible.bat wrapper sets HUB_LAUNCH_MODE=console; the default
rem stays silent (pythonw + console_tail.py).
if /i "%HUB_LAUNCH_MODE%"=="console" goto use_python

rem ---- prefer pythonw (GUI subsystem: no console window) ----
if exist "%HERE%python\pythonw.exe" goto pyw_bundled
if exist "%HERE%.venv\Scripts\pythonw.exe" goto pyw_venv
pythonw --version >nul 2>nul
if not errorlevel 1 goto pyw_path
pyw --version >nul 2>nul
if not errorlevel 1 goto pyw_path_pyw
goto use_python

:pyw_bundled
set "PYW=%HERE%python\pythonw.exe"
goto run_silent

:pyw_venv
set "PYW=%HERE%.venv\Scripts\pythonw.exe"
goto run_silent

:pyw_path
set "PYW=pythonw"
goto run_silent

:pyw_path_pyw
set "PYW=pyw"
goto run_silent

:run_silent
if not exist "%LOGDIR%" mkdir "%LOGDIR%" >nul 2>nul
rem The headless hub writes its output to the log file, so a refused
rem start ("a hub already runs on this port") would otherwise leave
rem this window vanishing with no explanation - it reads exactly
rem like a crash. Ask first, in THIS window, reusing the same check
rem the service runs (0 = running, 3 = free).
call :find_console_python
if not defined VIEWERPY goto start_headless
"%VIEWERPY%" "%SCRIPT%" --probe-only --port %PORT%
if not errorlevel 3 goto refused_start

:start_headless
rem Launch the hub headless: no console window, nothing in the
rem taskbar, and closing any window never stops it.
start "" "%PYW%" "%SCRIPT%" %PYARGS% >> "%LOGFILE%" 2>&1

rem Open a live log viewer in its own console window. The viewer
rem waits quietly while the hub starts and closes itself when the
rem hub is shut down. pythonw has no window, so a console python
rem is needed for the viewer.
if not defined VIEWERPY exit /b 0
if not exist "%HERE%console_tail.py" exit /b 0
start "" "%VIEWERPY%" "%HERE%console_tail.py" --port %PORT%
exit /b 0

:refused_start
echo.
echo  Nothing was started from this window - read the lines above.
echo  To restart the copy that is already running, use
echo  restart-hub.bat (it stops the old one first).
echo.
pause
exit /b 0

:use_python
rem ===========================================================
rem  Visible-window launcher (fallback, pythonw unavailable)
rem ===========================================================
set "PYEXE="

rem ---- 1) bundled runtime (ships with the zip) ----
if not exist "%HERE%python\python.exe" goto try_path
"%HERE%python\python.exe" --version >nul 2>nul
if errorlevel 1 goto try_path
set "PYEXE=%HERE%python\python.exe"
goto run

:try_path
rem ---- 2) python on PATH ----
python --version >nul 2>nul
if errorlevel 1 goto try_py
set "PYEXE=python"
goto run

:try_py
rem ---- 3) py launcher ----
py --version >nul 2>nul
if errorlevel 1 goto try_codex
set "PYEXE=py"
goto run

:try_codex
rem ---- 4) Codex bundled runtimes ----
call :find_codex
if defined PYEXE goto run

echo [ERROR] No usable Python found.
echo.
echo Options:
echo   1. Use the packaged zip, which already contains python\
echo   2. Install Python 3.9+ from https://www.python.org/downloads/
echo      (tick "Add python.exe to PATH" during setup)
echo   3. Edit this file and set PYEXE to a full path, e.g.
echo        set "PYEXE=C:\Python312\python.exe"
echo.
pause
exit /b 1

:no_script
echo [ERROR] hub_proxy.py not found next to this script.
echo         expected: %SCRIPT%
echo.
pause
exit /b 1

:find_codex
if not exist "%USERPROFILE%\.cache\codex-runtimes" goto :eof
for /d %%D in ("%USERPROFILE%\.cache\codex-runtimes\*") do call :probe "%%~D"
goto :eof

:probe
if defined PYEXE goto :eof
for /f "delims=" %%P in ('dir /b /s "%~1\python.exe" 2^>nul') do call :probe_one "%%~P"
goto :eof

:probe_one
if defined PYEXE goto :eof
"%~1" --version >nul 2>nul
if errorlevel 1 goto :eof
set "PYEXE=%~1"
goto :eof

:run
echo ===========================================================
echo   Fusion hub (WorkBuddy + Qoder + ZCode)
echo.
echo   API      : http://127.0.0.1:%PORT%/v1
echo   Dashboard: http://127.0.0.1:%PORT%/
echo   Upstreams: WorkBuddy / Qoder / ZCode (this process)
if /i "%HUB_LAUNCH_LAN%"=="1" echo   Mode     : LAN (0.0.0.0, API key required)
echo.
echo   Python   : %PYEXE%
echo   Log      : %LOGFILE%
echo.
echo   Keep this window open. Closing it stops the hub.
echo   Press Ctrl+C to stop.
echo ===========================================================
echo.

"%PYEXE%" "%SCRIPT%" %PYARGS%

echo.
echo [hub exited]
pause
exit /b 0

rem ===========================================================
rem  Console python lookup, shared by the start-up probe and the
rem  log viewer. Invoked with "call :find_console_python"; leaves
rem  VIEWERPY unset when no usable console python exists.
rem ===========================================================
:find_console_python
set "VIEWERPY="
if exist "%HERE%python\python.exe" goto fcp_bundled
if exist "%HERE%.venv\Scripts\python.exe" goto fcp_venv
python --version >nul 2>nul
if not errorlevel 1 goto fcp_path
py --version >nul 2>nul
if not errorlevel 1 goto fcp_py
goto :eof

:fcp_bundled
set "VIEWERPY=%HERE%python\python.exe"
goto :eof

:fcp_venv
set "VIEWERPY=%HERE%.venv\Scripts\python.exe"
goto :eof

:fcp_path
set "VIEWERPY=python"
goto :eof

:fcp_py
set "VIEWERPY=py"
goto :eof
