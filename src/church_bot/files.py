"""所有設定檔、名單的寫入都走這裡：先寫暫存檔再改名（寫到一半斷電也不會變成壞檔），
存完順便留一筆變更紀錄（見 core/versions.py；只記設定、三張表和牧區清單）。
"""

from __future__ import annotations

from pathlib import Path


def write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    from church_bot.core import versions

    store = versions.store_for(path)
    before = path.read_bytes() if store is not None and path.exists() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding=encoding)
    tmp.replace(path)
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
    tmp.replace(path)
    if store is not None:
        store.record(path, before, data)
