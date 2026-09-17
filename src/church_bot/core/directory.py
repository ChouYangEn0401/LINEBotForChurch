"""人員對照：服事表上的名字 → 同工名單裡的哪一位。

對不到人「不會」擋住提醒：訊息照服事表原本的寫法送出，同時回報給管理員，並猜可能是誰。
比對規則（由嚴到寬）：
1. 名字或「其他寫法」完全相同（忽略空白、全形半形、大小寫）
2. 去掉括號註記再比：「小明(代)」→ 找「小明」，顯示成「王小明(代)」
3. 都對不到 → 猜測：名字互相包含（小明 ↔ 王小明）或字形相近（打錯字）
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Iterable

from church_bot.models import Member, Person

_ANNOTATION_RE = re.compile(r"^(?P<base>.+?)\s*(?P<ann>[(（\[【].*[)）\]】])\s*$")


def normalize_name(name: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", name or "")).lower()


def split_annotation(raw: str) -> tuple[str, str]:
    """「小明(代)」→ ("小明", "(代)")；沒有括號 → (原字串, "")。"""
    text = (raw or "").strip()
    if m := _ANNOTATION_RE.match(text):
        return m["base"].strip(), m["ann"]
    return text, ""


class Directory:
    def __init__(self, members: Iterable[Member]) -> None:
        self.members = list(members)
        self._index: dict[str, Member] = {}
        for m in self.members:
            for key in (m.name, *m.aliases):
                norm = normalize_name(key)
                if norm:
                    self._index.setdefault(norm, m)  # 重複時第一位優先（tables 會另外警告）

    @property
    def is_empty(self) -> bool:
        return not self.members

    def lookup(self, name: str) -> Member | None:
        return self._index.get(normalize_name(name))

    def resolve(self, raw: str) -> Person:
        if member := self.lookup(raw):
            return Person(raw=raw, display=member.name, member=member)
        base, annotation = split_annotation(raw)
        if annotation and (member := self.lookup(base)):
            return Person(raw=raw, display=f"{member.name}{annotation}", member=member)
        return Person(raw=raw, display=raw, member=None, suggestions=self.suggest(base))

    def suggest(self, name: str, limit: int = 3) -> tuple[str, ...]:
        key = normalize_name(name)
        if not key:
            return ()
        found: list[str] = []
        if len(key) >= 2:
            found += [m.name for k, m in self._index.items() if len(k) >= 2 and (key in k or k in key)]
        found += [self._index[k].name for k in difflib.get_close_matches(key, list(self._index), n=limit, cutoff=0.5)]
        return tuple(dict.fromkeys(found))[:limit]
