@echo off
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "Remove-Item -ErrorAction SilentlyContinue ([Environment]::GetFolderPath('Startup') + '\church-bot.lnk')"
echo [OK] 已取消開機自動執行。如果程式現在還開著，關掉那個視窗就會停止。
pause
