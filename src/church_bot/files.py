"""所有設定檔、名單的寫入都走這裡：先寫暫存檔再改名（寫到一半斷電也不會變成壞檔）。

集中在一個地方寫，之後要加「每次存檔都留一份紀錄」這種事只要改這裡。
"""

from __future__ import annotations

from pathlib import Path


def write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding=encoding)
    tmp.replace(path)
