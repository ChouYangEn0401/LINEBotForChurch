"""所有設定檔、名單的寫入都走這裡：先寫暫存檔再改名（寫到一半斷電也不會變成壞檔），
存完順便留一筆變更紀錄（見 core/versions.py；只記設定、三張表和牧區清單）。
"""

from __future__ import annotations

import time
from pathlib import Path

from church_bot.errors import FileLockedError

SWAP_TRIES = 20  # 每次等 0.05 秒，最多等大約 1 秒
SWAP_WAIT = 0.05


def swap(tmp: Path, path: Path) -> None:
    """暫存檔換成正式的檔案。

    Windows 上只要有人剛好開著目標檔（另一條執行緒正在讀 .env、防毒軟體在掃剛寫好的檔案），換檔就會被拒絕
    一下下：等一下再試。一直被拒（例如 Excel 開著）才清掉暫存檔、說清楚，原本的檔案不動。
    """
    for _ in range(SWAP_TRIES):
        try:
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


def write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    from church_bot.core import versions

    store = versions.store_for(path)
    before = path.read_bytes() if store is not None and path.exists() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding=encoding)
    swap(tmp, path)
    if store is not None:
        store.record(path, before, path.read_bytes())


def write_bytes(path: Path, data: bytes) -> None:
    """原封不動寫回去（還原舊版本用），一樣留紀錄。"""
    from church_bot.core import versions

    store = versions.store_for(path)
    before = path.read_bytes() if store is not None and path.exists() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    swap(tmp, path)
    if store is not None:
        store.record(path, before, data)
