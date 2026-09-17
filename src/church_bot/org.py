"""🧪 實驗功能：大教會架構（牧區 → 區 → 小組 → LINE 群組 / 同工）。

目前只用來「看清楚全貌」：每個單位底下有哪些 LINE 群組、多少人、每個月大概會用掉多少 LINE 則數。
**不會改變實際發送的方式**——發送仍然只看 LINE 群組頁的設定。想法與下一步見 docs/LAB_LARGE_CHURCH.md。

資料存在 ``config/org.csv``，一列一個單位，Excel 也能改：

    單位名稱,上層單位,類型,負責人,LINE群組,同工,備註
    北區,,牧區,王大衛牧師,北區同工群,,
    北區第一小組,北區,小組,陳小明,北一小組群,陳小明、林美華,

* 「上層單位」空白 = 最上層；層數不限（有的教會是 牧區 → 區 → 小組，有的只有 小組）。
* 「LINE群組」填 LINE 群組頁上的群組名稱；「同工」填同工名單上的名字或其他寫法，多個用「、」隔開。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from church_bot.core.directory import Directory
from church_bot.models import Issue, Member, Severity, Target
from church_bot.tables import LINE_ID_RE, Column, CsvTable, TableResult, fmt_list, parse_list

KIND_SUGGESTIONS = ("牧區", "區", "小組", "團契", "事工團隊")
WEEKS_PER_MONTH = 52 / 12
PLAN_LIMITS = ((200, "輕用量（免費，每月 200 則）"), (3000, "中用量（每月 3,000 則）"), (6000, "高用量（每月 6,000 則）"))


@dataclass(frozen=True, slots=True)
class OrgUnit:
    name: str
    parent: str = ""
    kind: str = ""
    leader: str = ""
    groups: tuple[str, ...] = ()  # LINE 群組頁上的群組名稱
    members: tuple[str, ...] = ()  # 同工名單上的名字或其他寫法
    note: str = ""


class OrgTable(CsvTable[OrgUnit]):
    title = "組織架構"
    columns = (
        Column("name", "單位名稱", ("名稱", "單位", "name"), required=True),
        Column("parent", "上層單位", ("上層", "隸屬", "parent")),
        Column("kind", "類型", ("層級", "kind")),
        Column("leader", "負責人", ("區長", "組長", "leader")),
        Column("groups", "LINE群組", ("LINE 群組", "群組", "groups")),
        Column("members", "同工", ("成員", "組員", "members")),
        Column("note", "備註", ("說明", "note")),
    )

    def load(self) -> TableResult[OrgUnit]:
        if not self.path.exists():
            return TableResult([], [])  # 實驗功能：沒建立過不算問題
        return super().load()

    def from_row(self, row: dict[str, str], line_no: int, issues: list[Issue]) -> OrgUnit | None:
        name = row.get("name", "")
        if not name:
            issues.append(self._issue(Severity.WARNING, "org_no_name", f"第 {line_no} 列沒有填單位名稱，已略過"))
            return None
        return OrgUnit(name=name, parent=row.get("parent", ""), kind=row.get("kind", ""), leader=row.get("leader", ""),
                       groups=parse_list(row.get("groups", "")), members=parse_list(row.get("members", "")),
                       note=row.get("note", ""))

    def to_row(self, u: OrgUnit) -> list[str]:
        return [u.name, u.parent, u.kind, u.leader, fmt_list(u.groups), fmt_list(u.members), u.note]

    def validate_all(self, items: list[OrgUnit], issues: list[Issue]) -> None:
        seen: set[str] = set()
        for unit in items:
            if unit.name in seen:
                issues.append(self._issue(Severity.WARNING, "org_dup_name", f"「{unit.name}」出現了兩次，只採用第一個"))
            seen.add(unit.name)


# --------------------------------------------------------------------------- tree


@dataclass(slots=True)
class GroupInfo:
    target: Target
    size: int | None  # 群組人數（不含機器人）；還沒查過 = None

    @property
    def active(self) -> bool:
        return self.target.enabled and bool(LINE_ID_RE.match(self.target.line_id))


@dataclass(slots=True)
class Totals:
    groups: int = 0
    active_groups: int = 0
    people: int = 0  # 會收到提醒的群組人數加總（已知的部分）
    unknown_sizes: int = 0  # 會收到提醒、但還不知道人數的群組
    members: int = 0  # 不重複的同工人數
    monthly: int = 0  # 預估每月則數


@dataclass(slots=True)
class OrgNode:
    unit: OrgUnit
    depth: int
    groups: list[GroupInfo] = field(default_factory=list)
    members: list[str] = field(default_factory=list)  # 對到同工名單後的正式名字
    children: list["OrgNode"] = field(default_factory=list)
    totals: Totals = field(default_factory=Totals)  # 含所有下層


@dataclass(slots=True)
class OrgReport:
    roots: list[OrgNode]
    unassigned_groups: list[GroupInfo]
    unassigned_members: list[str]
    issues: list[Issue]
    sends_per_month: float
    church: Totals  # 全教會（包含還沒分配到單位的群組）

    @property
    def plan(self) -> str:
        return suggest_plan(self.church.monthly)

    def group_owners(self) -> dict[str, str]:
        """群組名稱 → 它目前所在的單位。"""
        owners: dict[str, str] = {}

        def walk(nodes: list[OrgNode]) -> None:
            for node in nodes:
                owners.update({g.target.name: node.unit.name for g in node.groups})
                walk(node.children)

        walk(self.roots)
        return owners

    def descendants(self, name: str) -> set[str]:
        """某個單位底下所有單位的名字（不含自己）；編輯時用來避免把上層設成自己的下層。"""
        found: set[str] = set()

        def walk(nodes: list[OrgNode], inside: bool) -> None:
            for node in nodes:
                if inside:
                    found.add(node.unit.name)
                walk(node.children, inside or node.unit.name == name)

        walk(self.roots, False)
        return found


def sends_per_month(every_n_weeks: int) -> float:
    return WEEKS_PER_MONTH / max(every_n_weeks, 1)


def suggest_plan(monthly: int) -> str:
    for limit, plan in PLAN_LIMITS:
        if monthly <= limit:
            return plan
    return "超過高用量的 6,000 則，需要另外加購訊息"


def _issue(severity: Severity, code: str, message: str, hint: str = "") -> Issue:
    return Issue(severity, code, f"【組織架構】{message}", hint)


def build_org(units: list[OrgUnit], targets: list[Target], members: list[Member], sizes: dict[str, int],
              every_n_weeks: int = 1) -> OrgReport:
    """``sizes``：群組 LINE ID → 人數（History.member_counts）；個人 ID（U 開頭）一律算 1 人。"""
    issues: list[Issue] = []
    per_month = sends_per_month(every_n_weeks)
    targets_by_name = {t.name: t for t in targets}
    directory = Directory(members)

    by_name: dict[str, OrgUnit] = {}
    for unit in units:
        by_name.setdefault(unit.name, unit)

    def size_of(target: Target) -> int | None:
        return 1 if target.line_id.startswith("U") else sizes.get(target.line_id)

    # 上層單位：找不到 → 當最上層；繞圈（A 的上層是 B、B 的上層是 A）→ 也當最上層
    parent_of: dict[str, str] = {}
    for unit in by_name.values():
        parent = unit.parent
        if parent and parent not in by_name:
            issues.append(_issue(Severity.WARNING, "org_missing_parent",
                                 f"「{unit.name}」的上層單位「{parent}」不存在，先當成最上層"))
            parent = ""
        parent_of[unit.name] = parent
    for name in by_name:
        seen, current = {name}, parent_of[name]
        while current:
            if current in seen:
                issues.append(_issue(Severity.ERROR, "org_cycle", f"「{name}」的上層單位繞了一圈回到自己，先當成最上層",
                                     "請檢查「上層單位」有沒有互相指來指去。"))
                parent_of[name] = ""
                break
            seen.add(current)
            current = parent_of[current]

    group_owner: dict[str, str] = {}
    nodes: dict[str, OrgNode] = {}
    for unit in by_name.values():
        node = OrgNode(unit, depth=0)
        for group_name in unit.groups:
            target = targets_by_name.get(group_name)
            if target is None:
                issues.append(_issue(Severity.WARNING, "org_unknown_group",
                                     f"「{unit.name}」的群組「{group_name}」不在 LINE 群組頁上"))
            elif group_name in group_owner:
                issues.append(_issue(Severity.WARNING, "org_group_twice",
                                     f"群組「{group_name}」同時在「{group_owner[group_name]}」和「{unit.name}」，只算在前者",
                                     "一個群組只放在一個單位，人數和則數才不會重複計算。"))
            else:
                group_owner[group_name] = unit.name
                node.groups.append(GroupInfo(target, size_of(target)))
        for raw in unit.members:
            member = directory.lookup(raw)
            if member is None:
                issues.append(_issue(Severity.WARNING, "org_unknown_member",
                                     f"「{unit.name}」的同工「{raw}」不在同工名單上"))
            elif member.name not in node.members:
                node.members.append(member.name)
        nodes[unit.name] = node

    roots: list[OrgNode] = []
    for name, node in nodes.items():
        parent = parent_of[name]
        (nodes[parent].children if parent else roots).append(node)

    def roll_up(node: OrgNode, depth: int) -> set[str]:
        """算出含下層的合計，回傳這個單位（含下層）的同工名字，給上層去重複用。"""
        node.depth = depth
        names = set(node.members)
        for child in node.children:
            names |= roll_up(child, depth + 1)
        node.totals = _totals(_all_groups(node), len(names), per_month)
        return names

    for root in roots:
        roll_up(root, 0)

    all_infos = [GroupInfo(t, size_of(t)) for t in targets]
    assigned_members = {name for node in nodes.values() for name in node.members}
    return OrgReport(
        roots=roots,
        unassigned_groups=[g for g in all_infos if g.target.name not in group_owner],
        unassigned_members=[m.name for m in members if m.active and m.name not in assigned_members],
        issues=issues,
        sends_per_month=per_month,
        church=_totals(all_infos, len([m for m in members if m.active]), per_month),
    )


def _all_groups(node: OrgNode) -> list[GroupInfo]:
    return [*node.groups, *(g for child in node.children for g in _all_groups(child))]


def _totals(groups: list[GroupInfo], member_count: int, per_month: float) -> Totals:
    active = [g for g in groups if g.active]
    people = sum(g.size or 0 for g in active)
    return Totals(groups=len(groups), active_groups=len(active), people=people,
                  unknown_sizes=sum(1 for g in active if g.size is None), members=member_count,
                  monthly=round(people * per_month))
