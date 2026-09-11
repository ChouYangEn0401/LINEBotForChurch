"""資料來源介面。

想換成別的來源（Excel 檔、Notion、資料庫…）只要實作 ``RosterSource``：
``describe()`` 回傳給人看的說明，``fetch()`` 回傳一或多張原始表格（全部字串）。
解析、對照、發送都不需要改。
"""

from __future__ import annotations

import re
from typing import Protocol, runtime_checkable

from church_bot.models import RawSheet


@runtime_checkable
class RosterSource(Protocol):
    def describe(self) -> str: ...

    def fetch(self) -> list[RawSheet]:
        """讀取失敗一律丟 ``SourceError``（帶中文 hint），不要回傳空資料假裝成功。"""
        ...


def split_worksheets(spec: str) -> list[str]:
    """「9月, 10月」→ ["9月", "10月"]。空字串 → []（代表第一個分頁）。"""
    return [p.strip() for p in re.split(r"[,，、;；\n]+", spec or "") if p.strip()]
