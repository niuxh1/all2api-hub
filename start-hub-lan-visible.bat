@echo off
rem ===========================================================
rem  Fusion hub - LAN mode, visible window
rem
rem  Same as start-hub-lan.bat, but the hub runs in THIS console
rem  window; closing the window stops it.
rem
rem  Usage: start-hub-lan-visible.bat [port] [api-key]
rem ===========================================================
setlocal
set "HUB_LAUNCH_MODE=console"
call "%~dp0start-hub-lan.bat" %*
