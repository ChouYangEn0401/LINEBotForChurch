@echo off
rem nssm stop for LINE chat commands (Cloudflare tunnel, service church-bot-webhook).
rem Same as the tray icon menu. The double-click way (no service) is still 3-open-webhook.bat.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0service\service.ps1" -Action stop -Target webhook
