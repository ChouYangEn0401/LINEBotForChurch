"""人員對照：服事表上的名字 → 同工名單裡的哪一位，或小團名單裡的哪一團。

對不到人「不會」擋住提醒：訊息照服事表原本的寫法送出，同時回報給管理員，並猜可能是誰。
比對規則（由嚴到寬）：
1. 同工的名字或「其他寫法」完全相同（忽略空白、全形半形、大小寫）；一個人可以有很多個寫法
2. 小團的團名或「其他寫法」完全相同 → 展開成那一團的成員
3. 去掉括號註記再比：「小明(代)」→ 找「小明」，顯示成「王小明(代)」
4. 都對不到 → 猜測：名字互相包含（小明 ↔ 王小明）或字形相近（打錯字）

同一個寫法同時是某位同工、又是某個小團時，以同工為準（``validate_teams`` 會警告，請改掉其中一個）。
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Iterable

from church_bot.models import Issue, Member, Person, Severity, Team

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
    def __init__(self, members: Iterable[Member], teams: Iterable[Team] = ()) -> None:
        self.members = list(members)
        self.teams = list(teams)
        self._index: dict[str, Member] = {}
        for m in self.members:
            for key in (m.name, *m.aliases):
                norm = normalize_name(key)
                if norm:
                    self._index.setdefault(norm, m)  # 重複時第一位優先（tables 會另外警告）
        self._team_index: dict[str, Team] = {}
        for t in self.teams:
            for key in (t.name, *t.aliases):
                norm = normalize_name(key)
                if norm and norm not in self._index:  # 撞到同工的名字時以同工為準
                    self._team_index.setdefault(norm, t)

    @property
    def is_empty(self) -> bool:
        return not self.members

    def lookup(self, name: str) -> Member | None:
        return self._index.get(normalize_name(name))

    def lookup_team(self, name: str) -> Team | None:
        return self._team_index.get(normalize_name(name))

    def resolve(self, raw: str) -> Person:
        person = self._resolve_member(raw)
        if person.matched:
            return person
        if team := self.lookup_team(raw):
            return self._resolve_team(raw, team.name, team)
        base, annotation = split_annotation(raw)
        if annotation and (team := self.lookup_team(base)):
            return self._resolve_team(raw, f"{team.name}{annotation}", team)
        return person

    def _resolve_member(self, raw: str, via_team: str = "") -> Person:
        """只對同工名單（不會展開小團），對不到就附上猜測。"""
        if member := self.lookup(raw):
            return Person(raw=raw, display=member.name, member=member, via_team=via_team)
        base, annotation = split_annotation(raw)
        if annotation and (member := self.lookup(base)):
            return Person(raw=raw, display=f"{member.name}{annotation}", member=member, via_team=via_team)
        return Person(raw=raw, display=raw, member=None, suggestions=self.suggest(base), via_team=via_team)

    def _resolve_team(self, raw: str, display: str, team: Team) -> Person:
        """小團成員只對同工名單，不會再展開成另一個小團（小團裡放小團也不會繞不完）。"""
        people = tuple(self._resolve_member(name, via_team=team.name) for name in team.members)
        return Person(raw=raw, display=display, team=team, team_people=people)

    def suggest(self, name: str, limit: int = 3) -> tuple[str, ...]:
        key = normalize_name(name)
        if not key:
            return ()
        pool = {**{k: m.name for k, m in self._index.items()}, **{k: t.name for k, t in self._team_index.items()}}
        found: list[str] = []
        if len(key) >= 2:
            found += [name for k, name in pool.items() if len(k) >= 2 and (key in k or k in key)]
        if len(key) >= 3:
            # 字形相近（打錯字）只在三個字以上才猜：中文姓名常見字重複很多（「建」「國」「心」…），
            # 兩個字只要共用一個字，difflib 的比對分數就會落在門檻上，很容易猜錯成完全不相干的人
            # （例如「國良」被猜成「蔡建國」、「雅心」被猜成「許心怡」）。子字串包含（上面那段）不受影響，
            # 「以諾」對到「王以諾」這種去掉姓的暱稱還是猜得到。
            found += [pool[k] for k in difflib.get_close_matches(key, list(pool), n=limit, cutoff=0.5)]
        return tuple(dict.fromkeys(found))[:limit]


# --------------------------------------------------------------------------- 小團名單的檢查


def validate_teams(teams: Iterable[Team], members: Iterable[Member]) -> list[Issue]:
    """小團名單跟同工名單合起來看才知道的問題（成員不在名單上、團名撞到人名…）。

    都只是警告：提醒照樣會發，只是對不到的成員沒辦法被 @，會照小團名單上的寫法顯示。
    """
    directory = Directory(members)
    owner = {normalize_name(key): m.name for m in directory.members for key in (m.name, *m.aliases)}
    issues: list[Issue] = []
    for team in teams:
        for key in (team.name, *team.aliases):
            if who := owner.get(normalize_name(key)):
                issues.append(Issue(
                    Severity.WARNING, "team_name_clash",
                    f"【小團】「{key}」同時是同工「{who}」的名字或其他寫法，服事表寫這個只會對到那個人",
                    "請把小團或同工其中一邊的寫法改掉。",
                ))
        if not team.members:
            issues.append(Issue(
                Severity.WARNING, "team_empty", f"【小團】「{team.name}」還沒填成員，提醒裡只會顯示團名",
                "到「同工名單 → 小團」把成員填上，提醒才會列出人、也才 @ 得到。",
            ))
        for raw in team.members:
            if directory.lookup(raw) is None:
                issues.append(Issue(
                    Severity.WARNING, "team_unknown_member", f"【小團】「{team.name}」的成員「{raw}」不在同工名單上",
                    "提醒裡會照這個寫法顯示，但沒辦法 @ 他。到「同工名單」新增，或設成某位同工的「其他寫法」。",
                ))
    return issues
