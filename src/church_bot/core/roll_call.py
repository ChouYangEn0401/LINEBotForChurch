"""點名：服事表上「這個群組會提醒到的人」，誰已經 @ 得到、誰登記了在等確認、誰還沒登記。

給 LINE 指令「/點名」用（見 webhook.py）。機器人沒辦法知道群組裡有哪些人（沒有認證的官方帳號拿不到成員清單），
所以從服事表出發：排到的人一個一個看——
* 已經 @ 得到：同工名單對應到他，而且有 LINE 帳號
* 等管理員確認：有人用「/我的名字」（或「/我的暱稱」）登記成這個名字，管理員還沒在網頁按「對應」
* 還沒登記：上面兩種都不是
小團展開成團員（團名本身不是人，不用登記）；同一個人排很多次只算一次。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from church_bot.core.accounts import LineAccount
from church_bot.core.directory import Directory, normalize_name
from church_bot.core.renderer import matches_any, select_assignments
from church_bot.models import Roster, Target


@dataclass(slots=True)
class RollCall:
    first: dt.date | None = None  # 服事表從哪一天排到哪一天（只算今天以後）
    last: dt.date | None = None
    linked: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.linked) + len(self.pending) + len(self.missing)


def roll_call(roster: Roster, directory: Directory, target: Target | None, accounts: list[LineAccount],
              today: dt.date) -> RollCall:
    """``target`` = 在哪個群組點名（照它的「只發這些服事／聚會」篩）；None = 整份服事表。"""
    claimed: set[str] = set()  # 登記了、還沒處理的名字（本人打的名字、暱稱，以及它對到的同工）
    for account in accounts:
        if account.member is not None or account.ignored:
            continue
        claimed.update(normalize_name(n) for n in (account.real_name, *account.nicknames) if n)
        if account.match is not None and account.matched_by == "real_name":
            claimed.add(normalize_name(account.match.name))

    result = RollCall()
    seen: set[str] = set()
    for day in roster.days:
        if day.date < today:
            continue
        if target is not None and target.labels and not matches_any(day.label, target.labels):
            continue
        assignments = select_assignments(day, target)
        if not assignments:
            continue
        result.first = result.first or day.date
        result.last = day.date
        for assignment in assignments:
            for raw in assignment.names:
                for person in directory.resolve(raw).individuals:
                    name = person.member.name if person.member else person.display
                    key = normalize_name(name)
                    if key in seen:
                        continue
                    seen.add(key)
                    if person.member is not None and person.member.line_user_id:
                        result.linked.append(name)
                    elif key in claimed or normalize_name(person.raw) in claimed:
                        result.pending.append(name)
                    else:
                        result.missing.append(name)
    return result
