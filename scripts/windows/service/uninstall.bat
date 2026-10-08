@echo off
rem Unregister the service and go back to the double-click 2-start way of running.
rem All the logic (and the Chinese messages) lives in service.ps1: cmd.exe mangles UTF-8
rem batch files once they get big, so these launchers stay plain ASCII and one line long.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0service.ps1" -Action uninstall
