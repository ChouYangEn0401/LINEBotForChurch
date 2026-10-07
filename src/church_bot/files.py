"""所有設定檔、名單的寫入都走這裡：先寫暫存檔再改名（寫到一半斷電也不會變成壞檔），
存完順便留一筆變更紀錄（見 core/versions.py；只記設定、三張表和牧區清單）。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from church_bot.errors import FileLockedError

SWAP_TRIES = 20  # 每次等 0.05 秒，最多等大約 1 秒
SWAP_WAIT = 0.05
# 同一個程式裡讀檔和換檔排隊：Windows 上「有人開著檔案」時換不過去、「正在換」時又打不開。
# 管理網頁每個請求都會讀 .env，有人同時在存密碼就會撞到；排隊之後同一個程式裡兩邊永遠不會撞。
# 跟別的程式（cli.bat、防毒軟體）撞到的少數情況，靠下面的「等一下再試」。
_IO_LOCK = threading.RLock()


def swap(tmp: Path, path: Path) -> None:
    """暫存檔換成正式的檔案。

    跟同一個程式裡的讀檔排隊（見 _IO_LOCK）；別的程式剛好開著目標檔（防毒軟體在掃剛寫好的檔案…）
    就等一下再試。一直被拒（例如 Excel 開著）才清掉暫存檔、說清楚，原本的檔案不動。
    """
    for _ in range(SWAP_TRIES):
        try:
            with _IO_LOCK:
                tmp.replace(path)
            return
        except PermissionError as exc:
            error = exc
            time.sleep(SWAP_WAIT)
    tmp.unlink(missing_ok=True)
    raise FileLockedError(
        f"{path.name} 存不進去：這個檔案正被別的程式打開（多半是 Excel）",
        "請先關掉 Excel（或其他開著這個檔案的程式），再做一次。剛剛的修改還沒有存。",
    ) from error


def read_bytes(path: Path) -> bytes:
    """讀檔。跟同一個程式裡的換檔排隊（見 _IO_LOCK）；別的程式剛好在換這個檔案時，開檔會被拒絕一下下：等一下再試。

    例如有人在存網站密碼的同時，另一個人打開網頁（每個請求都會讀 .env）。一直被拒才說清楚。
    """
    for _ in range(SWAP_TRIES):
        try:
            with _IO_LOCK:
                return path.read_bytes()
        except PermissionError as exc:
            error = exc
            time.sleep(SWAP_WAIT)
    raise FileLockedError(f"讀不到 {path.name}：這個檔案正被別的程式鎖住",
                          "等一下再試；一直這樣的話，關掉開著這個檔案的程式。") from error


def read_text(path: Path, encoding: str = "utf-8-sig") -> str:
    """同 Path.read_text（換行一律變成 LF），但會等存檔的那一瞬間過去（見 read_bytes）。"""
    return read_bytes(path).decode(encoding).replace("\r\n", "\n").replace("\r", "\n")


def write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    from church_bot.core import versions

    store = versions.store_for(path)
    before = read_bytes(path) if store is not None and path.exists() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding=encoding)
    swap(tmp, path)
    if store is not None:
        store.record(path, before, read_bytes(path))


def write_bytes(path: Path, data: bytes) -> None:
    """原封不動寫回去（還原舊版本用），一樣留紀錄。"""
    from church_bot.core import versions

    store = versions.store_for(path)
    before = read_bytes(path) if store is not None and path.exists() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    swap(tmp, path)
    if store is not None:
        store.record(path, before, data)
