@echo off
rem For the Telegram bot (it does the scheduling) and other programs: no questions, no "press any key".
rem Runs one command and exits. Exit code: 0 = OK, 1 = something needs attention, 2 = settings problem.
rem   cli.bat send --retries 3 --retry-wait 300 --popup
rem                              weekly job: send (Push); content already sent is skipped; retry temporary
rem                              failures 3 times, 5 minutes apart; pop up a window if it still fails
rem   cli.bat send               send now, once
rem   cli.bat preview            show what would be sent (sends nothing)
rem   cli.bat check              health check + preview
rem   cli.bat quota              this month's LINE usage; schedule it ~5 min after send so the
rem                              management page shows the usage *after* that send (LINE counts lag)
rem   cli.bat 狀態 --telegram  every ministry at a glance: roster end date, last send, next send,
rem                              LINE usage. --telegram also pushes it to the server manager's phone;
rem                              --quick skips reading the roster (no network); --short one line each
rem For ClawBot (Telegram /line panel); full contract in docs\CLI.md. Add --json for one JSON line,
rem --ministry=m1 when there is more than one ministry, --by=telegram:ID on every command that writes:
rem   cli.bat roster show        the roster, 2 weeks back + 4 weeks ahead, swaps applied (free)
rem   cli.bat roster image --out=C:\path\roster.png     same, drawn as one PNG (free)
rem   cli.bat swap person --date=2026-09-13 --role=X --from=A --to=B --by=...
rem                              swap one person on one day (stored here, Google Sheet untouched; free)
rem   cli.bat swap weeks --a=DATE --b=DATE [--role=X or --column=X] --by=...   swap two days (free)
rem   cli.bat swap list [--all]  /  cli.bat swap undo --id=ID --by=...
rem   cli.bat remind now --by=... [--target=GROUP] [--dry-run]
rem                              push this week's reminder now (= send --force, this ministry only);
rem                              COSTS LINE MESSAGES unless --dry-run
rem   cli.bat quota --json  /  cli.bat ministries list --json
setlocal
call "%~dp0_env.bat"
if not exist "%VENV_PY%" (
  echo [X] 還沒安裝。請先雙擊 1-install.bat
  exit /b 2
)
"%VENV_PY%" -m church_bot %*
exit /b %errorlevel%
