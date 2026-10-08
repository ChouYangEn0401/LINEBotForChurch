"""兩個常駐程式（後台、LINE 指令那條臨時網址）跟右下角小圖示之間的聯絡方式。

擁有者 2026-10-09：後台變成常駐服務之後，要能在右下角的小圖示看到它在跑，也能從那裡結束它、
停／開／重開服務。不管是雙擊 2-start／3-open-webhook 開的，還是 Windows 服務開的，都一樣。

三樣東西，全部都不需要輪詢：

* **在跑的紀錄**（``data/run/<程式>.json``）：程式一開起來就寫下自己的 pid、怎麼開的（服務／雙擊）；
  正常結束時刪掉。小圖示拿 pid 去問 Windows 那個程式還在不在（對照建立時間，避免 pid 被別人重用）。
* **「請結束」的訊號**：一個有名字的 Windows 事件（event）。程式開一條執行緒「睡在上面」等，
  小圖示按「結束程式」就把它叫醒，程式照平常 Ctrl+C 的路收工（「後台已關閉」那一則照樣送）。
  服務是 SYSTEM 身分、在另一個桌面，所以事件放在 ``Global\\``，並且讓已登入的使用者可以按它——
  這樣結束程式不用跳 UAC，也不用砍程式。
* **服務狀態**：直接問 Windows 的服務控制器（psutil），不開 ``sc query`` 這種新程式。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from church_bot.config import Paths

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Program:
    key: str        # 檔名、事件名稱用
    service: str    # Windows 服務名稱（scripts/windows/service/service.ps1）
    title: str      # 小圖示上的名字
    launcher: str   # 雙擊開的那個 .bat


# 名字照擁有者 2026-10-09 定的家族慣例：圖示上是「專案字母＋元件記號」，提示文字以「專案 · 元件」開頭
# （這個專案是 LINE；ClawBot 那邊是 H／T）。以後別的專案也照這個排。
WEB = Program("web", "church-bot", "LINE · 後台", "2-start.bat")
WEBHOOK = Program("webhook", "church-bot-webhook", "LINE · webhook", "3-open-webhook.bat")
PROGRAMS = {p.key: p for p in (WEB, WEBHOOK)}

ENDED_FROM_TRAY = 5  # 臨時網址被小圖示結束：3-open-webhook.bat 看到這個就不停下來等按鍵


# --------------------------------------------------------------------------- 在跑的紀錄


def run_file(paths: Paths, program: Program) -> Path:
    return paths.church.root / "data" / "run" / f"{program.key}.json"


def _created(pid: int) -> float | None:
    try:
        import psutil

        return psutil.Process(pid).create_time()
    except Exception:  # noqa: BLE001 - 不在了、看不到，都當作不在
        return None


def mark_running(paths: Paths, program: Program, *, pid: int | None = None, detail: str = "") -> None:
    """程式開起來了：寫下 pid（預設自己）跟怎麼開的。寫不進去只記 log，絕對不能害程式開不起來。"""
    pid = pid or os.getpid()
    record = {"pid": pid, "created": _created(pid),
              "mode": "service" if os.environ.get("CHURCH_BOT_SERVICE") else "manual",
              "started_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"), "detail": detail}
    target = run_file(paths, program)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        log.warning("寫不下 %s：%s", target, exc)


def mark_stopped(paths: Paths, program: Program) -> None:
    """正常結束：刪掉紀錄。只刪自己的（重新啟動時新的那一個可能已經寫好了）。"""
    target = run_file(paths, program)
    try:
        if json.loads(target.read_text(encoding="utf-8")).get("pid") == os.getpid():
            target.unlink()
    except (OSError, ValueError):
        pass


def running(paths: Paths, program: Program) -> dict | None:
    """那個程式現在在跑嗎？在跑就回紀錄（pid、mode=service/manual、started_at、detail），不然 None。"""
    try:
        record = json.loads(run_file(paths, program).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or not isinstance(record.get("pid"), int):
        return None
    created = _created(record["pid"])
    if created is None:
        return None
    if isinstance(record.get("created"), (int, float)) and abs(created - record["created"]) > 2:
        return None  # 那個 pid 已經被別的程式拿去用了
    return record


# --------------------------------------------------------------------------- 服務狀態


def service_state(program: Program) -> str | None:
    """Windows 服務的狀態：running / stopped / start_pending / stop_pending / paused…；沒註冊回 None。"""
    if sys.platform != "win32":
        return None
    try:
        import psutil

        return psutil.win_service_get(program.service).status()
    except Exception:  # noqa: BLE001 - 沒註冊（NoSuchProcess）、看不到，都當作沒有
        return None


def service_app_alive(program: Program) -> bool:
    """服務開著的時候，nssm 底下真的有程式在跑嗎（不靠程式自己寫的紀錄）。

    只看 data/run/*.json 的話，舊版程式、或還沒寫好紀錄的那幾秒，都會被誤判成「沒在跑」——
    2026-10-09 擁有者就看到「服務開著、小圖示卻說沒在跑、又按不了啟動」。nssm 是服務的那個 pid，
    我們的程式是它的子程式；有子程式在＝程式在。看程式清單不用系統管理員權限。
    """
    if sys.platform != "win32":
        return False
    try:
        import psutil

        pid = psutil.win_service_get(program.service).pid()
        return bool(pid) and any(child.is_running() for child in psutil.Process(pid).children())
    except Exception:  # noqa: BLE001 - 看不到就當作不知道＝不在
        return False


# --------------------------------------------------------------------------- 有人登出 Windows

_CTRL_LOGOFF_EVENT = 5
_logoff_handler = None  # 要一直留著，不然 ctypes 的回呼會被回收、Windows 呼叫時整個程式掛掉


def ignore_logoff_when_service() -> None:
    """以服務身分跑的時候，「有人登出 Windows」不要把我們關掉。

    Windows 會把 CTRL_LOGOFF_EVENT 送給服務底下的 console 程式（任何一個使用者登出都會）；
    沒人接的話預設處理就是結束程式——服務等於被登出關掉，nssm 只好再開一次。
    （ClawBot 2026-10-09 踩到後提醒的。）只吃掉這一種，Ctrl+C 之類照舊交給 Python。
    """
    global _logoff_handler
    if sys.platform != "win32" or not os.environ.get("CHURCH_BOT_SERVICE") or _logoff_handler is not None:
        return
    import ctypes
    from ctypes import wintypes

    handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    _logoff_handler = handler_type(lambda event: event == _CTRL_LOGOFF_EVENT)
    if not ctypes.windll.kernel32.SetConsoleCtrlHandler(_logoff_handler, True):
        log.warning("沒辦法設定「登出時不要關掉」：%s", ctypes.get_last_error())


# --------------------------------------------------------------------------- 「請結束」的訊號

# 已登入的使用者（IU）可以等、可以按；SYSTEM、系統管理員、建立的人什麼都可以
_EVENT_SDDL = "D:(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;OW)(A;;0x00100002;;;IU)"
_SYNCHRONIZE_AND_MODIFY = 0x00100002
_INFINITE = 0xFFFFFFFF


def event_name(paths: Paths, program: Program, scope: str = "Global") -> str:
    # 帶上專案資料夾的雜湊：同一台電腦上另一份（例如 worktree）不會按到這一份
    digest = hashlib.sha1(str(paths.church.root.resolve()).lower().encode("utf-8")).hexdigest()[:10]
    return f"{scope}\\church-bot-end-{program.key}-{digest}"


def _kernel32():
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    k32.CreateEventW.restype = wintypes.HANDLE
    k32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k32.OpenEventW.restype = wintypes.HANDLE
    k32.SetEvent.argtypes = [wintypes.HANDLE]
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    return k32


def _security_attributes():
    import ctypes
    from ctypes import wintypes

    class SECURITY_ATTRIBUTES(ctypes.Structure):  # noqa: N801 - Windows 的名字
        _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                    ("bInheritHandle", wintypes.BOOL)]

    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    convert = adv.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    convert.restype = wintypes.BOOL
    descriptor = ctypes.c_void_p()
    if not convert(_EVENT_SDDL, 1, ctypes.byref(descriptor), None):
        return None
    attrs = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), descriptor, False)
    return attrs  # descriptor 不釋放：整個程式只建一兩次，跟著程式一起結束


def listen_for_end(paths: Paths, program: Program, on_end: Callable[[], None]) -> bool:
    """開一條執行緒睡在「請結束」事件上，被叫醒就呼叫 on_end()。不是輪詢：沒人按就一直睡。

    回傳有沒有成功建立（非 Windows、或建不起來就 False，程式照常跑，只是小圖示沒辦法結束它）。
    """
    if sys.platform != "win32":
        return False
    import ctypes

    k32 = _kernel32()
    attrs = _security_attributes()
    handle = None
    for scope in ("Global", "Local"):
        handle = k32.CreateEventW(ctypes.byref(attrs) if attrs else None, False, False,
                                  event_name(paths, program, scope))
        if handle:
            break
    if not handle:
        log.warning("建不起「請結束」的事件（%s），小圖示會沒辦法結束這個程式", ctypes.get_last_error())
        return False

    def wait() -> None:
        if k32.WaitForSingleObject(handle, _INFINITE) == 0:
            log.info("小圖示按了「結束程式」")
            on_end()

    threading.Thread(target=wait, name=f"end-{program.key}", daemon=True).start()
    return True


def request_end(paths: Paths, program: Program) -> bool:
    """小圖示按「結束程式」：叫醒那個程式。它沒在跑（事件不存在）就回 False。"""
    if sys.platform != "win32":
        return False
    k32 = _kernel32()
    for scope in ("Global", "Local"):
        handle = k32.OpenEventW(_SYNCHRONIZE_AND_MODIFY, False, event_name(paths, program, scope))
        if handle:
            try:
                return bool(k32.SetEvent(handle))
            finally:
                k32.CloseHandle(handle)
    return False
