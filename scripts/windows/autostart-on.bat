@echo off
call "%~dp0_env.bat"
if not exist "%VENV_PY%" (
  echo [X] 還沒安裝。請先雙擊 1-install.bat
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s = (New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup') + '\church-bot.lnk'); $s.TargetPath = '%~dp02-start.bat'; $s.Arguments = '--no-browser'; $s.WorkingDirectory = '%ROOT%'; $s.WindowStyle = 7; $s.Description = 'church-bot'; $s.Save()" || goto :fail
echo [OK] 已設定開機自動執行：登入 Windows 後，工作列會出現一個縮小的視窗，不要關掉它。
echo      取消請雙擊 autostart-off.bat
pause
exit /b 0
:fail
echo [X] 設定失敗，請把上面的訊息截圖給維護的人。
pause
exit /b 1
