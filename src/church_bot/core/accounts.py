"""LINE 帳號 ↔ 同工名單：誰已經對應好、誰登記了名字在等管理員確認、該按哪個按鈕。

同工名單（members.csv）的「名字」是服事表用的真實姓名；LINE 帳號（userId）是永久不變的 ID；
LINE 顯示名稱本人隨時會改。所以對應關係存在同工名單的 LINE_userId 欄，顯示名稱只拿來參考。

每個帳號會落在其中一種狀態：
* linked   — 已經對應到同工（登記的名字跟名單一樣，或沒有登記）
* rename   — 已經對應到同工，但本人登記了不同的名字 → 「改名」
* link     — 還沒對應，但登記的名字（或 LINE 名稱）就是名單上某位還沒有 LINE 帳號的同工 → 「對應」
* conflict — 登記的名字在名單上，但那位同工已經對應到另一個 LINE 帳號 → 要人工判斷
* add      — 名單上沒有這個人 → 「加入」成新同工
"""

from __future__ import annotations

from dataclasses import dataclass

from church_bot.core.directory import Directory, normalize_name
from church_bot.models import Member

ACTION_ORDER = {"rename": 0, "link": 1, "conflict": 2, "add": 3, "linked": 4}


@dataclass(frozen=True, slots=True)
class LineAccount:
    user_id: str
    display_name: str
    real_name: str  # 本人用「/我的名字」登記的名字
    real_name_at: str
    chat_names: str
    last_seen: str
    ignored: bool
    member: Member | None = None  # 用 userId 對應到的同工
    match: Member | None = None  # 還沒對應，但名字對得上的同工
    matched_by: str = ""  # "real_name" 或 "display_name"

    @property
    def action(self) -> str:
        if self.member is not None:
            same = not self.real_name or normalize_name(self.real_name) == normalize_name(self.member.name)
            return "linked" if same else "rename"
        if self.match is not None:
            return "conflict" if self.match.line_user_id else "link"
        return "add"

    @property
    def needs_review(self) -> bool:
        """本人登記了名字、而且管理員還沒處理。"""
        return bool(self.real_name) and self.action != "linked"

    @property
    def proposed_name(self) -> str:
        return self.real_name or self.display_name


def build_accounts(people: list[dict], members: list[Member]) -> list[LineAccount]:
    """``people`` 是 History.people() 的結果（已經照最後出現時間排好）。待處理的排前面。"""
    by_uid = {m.line_user_id: m for m in members if m.line_user_id}
    directory = Directory(members)
    accounts: list[LineAccount] = []
    for p in people:
        member = by_uid.get(p["user_id"])
        match, matched_by = None, ""
        if member is None:
            real = directory.lookup(p.get("real_name") or "") if p.get("real_name") else None
            shown = directory.lookup(p.get("display_name") or "") if p.get("display_name") else None
            if real is not None:
                match, matched_by = real, "real_name"
            elif shown is not None and not shown.line_user_id:
                # 只憑 LINE 名稱猜：猜到的人已經有帳號就不算（LINE 名稱撞名很常見，不要誤報衝突）
                match, matched_by = shown, "display_name"
        accounts.append(LineAccount(
            user_id=p["user_id"], display_name=p.get("display_name") or "", real_name=p.get("real_name") or "",
            real_name_at=p.get("real_name_at") or "", chat_names=p.get("chat_names") or "",
            last_seen=p.get("last_seen") or "", ignored=bool(p.get("ignored")),
            member=member, match=match, matched_by=matched_by,
        ))
    return sorted(accounts, key=lambda a: (not a.needs_review, ACTION_ORDER[a.action]))
