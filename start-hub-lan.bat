@echo off
rem ===========================================================
rem  Fusion hub - LAN mode
rem
rem  Listens on every network interface so phones, laptops and
rem  other PCs on the same network can use it.
rem
rem  An API key is REQUIRED in this mode: pass it as the second
rem  argument, or let the hub generate one on first start (it is
rem  printed in the log viewer / data\hub\logs\hub.log).
rem
rem  Usage: start-hub-lan.bat [port] [api-key]
rem
rem  The default stays silent (pythonw + log viewer); pair it
rem  with start-hub-lan-visible.bat for the close-window-to-stop
rem  behaviour.
rem ===========================================================
setlocal EnableExtensions
set "HUB_LAUNCH_LAN=1"
if not "%~2"=="" set "HUB_API_KEY=%~2"
call "%~dp0start-hub.bat" %1
