@echo off
rem nssm restart for the back end (admin web page + weekly reminders, service church-bot).
rem Same as the tray icon menu. The double-click way (no service) is still 2-start.bat.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0service\service.ps1" -Action restart -Target web
