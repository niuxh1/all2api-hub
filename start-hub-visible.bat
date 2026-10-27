@echo off
rem ===========================================================
rem  Fusion hub - visible-window launcher
rem
rem  Same service as start-hub.bat, but it runs in THIS console
rem  window; closing the window stops the hub.
rem
rem  The default launcher stays silent (pythonw + log viewer).
rem  Use this one when you want the old close-the-window-to-stop
rem  behaviour.
rem ===========================================================
setlocal
set "HUB_LAUNCH_MODE=console"
call "%~dp0start-hub.bat" %*
