"""Windows「工作排程器」：不用一直開著 2-start 視窗，每週也會自動提醒。

做法：Windows 每 15 分鐘在背景（沒有視窗）跑一次 ``church_bot send --scheduled``。
那個指令自己判斷「現在該不該發」（見 scheduler.scheduled_skip_reason）：沒到時間就什麼都不做，
到時間、還沒發過才發。所以：

* 發送時間、每幾週、自動發送開關，一律照設定檔（網頁或 LINE /設定 改了馬上生效，不用重設這個工作）
* 電腦關機或睡眠錯過了，開機後 15 分鐘內會補發（跟 2-start 一樣，超過 12 小時就不補）
* 發送失敗（例如剛睡醒、網路還沒連上）下一次檢查會再試，同一次最多試 3 次
* 同時開著 2-start 也沒關係，有防重複 + 跨程式的鎖（locking.py），不會發兩次

只在「使用者登入 Windows」時執行（不用存密碼）；專案資料夾搬家之後要重新設定一次。
"""

from __future__ import annotations

import base64
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from church_bot.config import Paths
from church_bot.errors import ConfigError

TASK_NAME = "church-bot-weekly"
CHECK_EVERY_MINUTES = 15
_NEVER_RAN = 267011  # SCHED_S_TASK_HAS_NOT_RUN
_RUNNING = 267009  # SCHED_S_TASK_RUNNING：正在跑，不是錯誤


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def task_command(paths: Paths) -> tuple[str, str]:
    """(要執行的程式, 參數)。用 pythonw.exe：背景執行、不會跳出黑色視窗。"""
    python = Path(sys.executable)
    pythonw = python.with_name("pythonw.exe")
    exe = pythonw if pythonw.exists() else python
    src = repr(str(paths.root / "src"))  # 程式碼在 src/，工作排程器沒辦法設 PYTHONPATH，所以在這裡加
    code = f"import sys; sys.path.insert(0, {src}); from church_bot.cli import main; sys.exit(main(['send', '--scheduled']))"
    return str(exe), f'-c "{code}"'


def install_script(paths: Paths) -> str:
    exe, args = task_command(paths)
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        f"$action = New-ScheduledTaskAction -Execute {_ps_quote(exe)} -Argument {_ps_quote(args)}"
        f" -WorkingDirectory {_ps_quote(str(paths.root))}",
        # 每天 00:00 開始、每 15 分鐘一次、持續一天 = 全天候每 15 分鐘（這個寫法在舊版 Windows 10 也能用）
        "$trigger = New-ScheduledTaskTrigger -Daily -At 00:00",
        "$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At 00:00"
        f" -RepetitionInterval (New-TimeSpan -Minutes {CHECK_EVERY_MINUTES})"
        " -RepetitionDuration (New-TimeSpan -Days 1)).Repetition",
        "$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries"
        " -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)",
        f"Register-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} -Action $action -Trigger $trigger"
        f" -Settings $settings -Description {_ps_quote('教會服事提醒：每 15 分鐘檢查一次，到發送時間就發（' + str(paths.root) + '）')}"
        " -Force | Out-Null",
    ])


STATUS_SCRIPT = "\n".join([
    f"$t = Get-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} -ErrorAction SilentlyContinue",
    "if (-not $t) { 'MISSING'; exit 0 }",
    "$i = $t | Get-ScheduledTaskInfo",
    "'STATE=' + $t.State",
    "'LAST=' + $i.LastRunTime.ToString('yyyy/MM/dd HH:mm')",
    "'RESULT=' + $i.LastTaskResult",
    "'NEXT=' + $i.NextRunTime.ToString('yyyy/MM/dd HH:mm')",
])

UNINSTALL_SCRIPT = f"Unregister-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} -Confirm:$false -ErrorAction SilentlyContinue"


def _powershell(script: str) -> str:
    if sys.platform != "win32":
        raise ConfigError("「每週自動發送（不用開視窗）」只支援 Windows",
                          "Mac 請用 autostart-on.sh 讓 2-start 開機自動執行。")
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
        capture_output=True, timeout=60,
    )
    out = result.stdout.decode("utf-8", errors="replace") if result.stdout else ""
    if result.returncode != 0:
        err = result.stderr.decode("mbcs" if sys.platform == "win32" else "utf-8", errors="replace").strip()
        raise ConfigError(f"設定 Windows 工作排程器失敗：{err.splitlines()[0] if err else result.returncode}",
                          "把這段訊息截圖給維護的人；也可以改用 2-start + autostart-on。")
    return out


@dataclass(frozen=True, slots=True)
class TaskStatus:
    state: str  # Ready / Running / Disabled
    last_run: str  # 從沒跑過 = ""
    last_ok: bool | None  # None = 從沒跑過或正在跑
    running: bool
    next_run: str


def install(paths: Paths) -> None:
    _powershell(install_script(paths))


def uninstall() -> None:
    _powershell(UNINSTALL_SCRIPT)


def status() -> TaskStatus | None:
    out = _powershell(STATUS_SCRIPT).strip()
    if not out or out.startswith("MISSING"):
        return None
    values = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    result = int(values.get("RESULT", "0") or 0)
    never = result == _NEVER_RAN
    running = result == _RUNNING
    return TaskStatus(
        state=values.get("STATE", "?"),
        last_run="" if never else values.get("LAST", ""),
        last_ok=None if never or running else result == 0,
        running=running,
        next_run=values.get("NEXT", ""),
    )
