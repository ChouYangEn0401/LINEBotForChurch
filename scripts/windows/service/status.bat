@echo off
rem Is it running? Service state plus every ministry's roster / last send / next send.
rem All the logic (and the Chinese messages) lives in service.ps1: cmd.exe mangles UTF-8
rem batch files once they get big, so these launchers stay plain ASCII and one line long.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0service.ps1" -Action status
