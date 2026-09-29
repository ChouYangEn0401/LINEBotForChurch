"""決定「這次要發給哪些群組、發什麼內容」，並找出所有需要讓人知道的問題。

原則：問題分三級
* ERROR   — 提醒送不出去或內容一定有錯（例如這週服事表沒資料）→ 通知管理員
* WARNING — 提醒會送，但有人該處理（例如名字對不到、服事表快用完）→ 通知管理員
* INFO    — 參考資訊，只顯示在網頁上
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from church_bot.config import BehaviorSettings
from church_bot.core.dates import format_date
from church_bot.core.directory import Directory
from church_bot.core.renderer import Renderer, fingerprint, matches_any, select_assignments
from church_bot.models import Issue, OutgoingMessage, Person, Roster, ServiceDay, Severity, Target
from church_bot.tables import LINE_ID_RE

DATE_FMT = "%-m/%-d（{weekday}）"


@dataclass(frozen=True, slots=True)
class PlannedMessage:
    target: Target
    day: ServiceDay
    message: OutgoingMessage
    fingerprint: str


@dataclass(slots=True)
class Plan:
    today: dt.date
    window_end: dt.date
    days: list[ServiceDay] = field(default_factory=list)
    messages: list[PlannedMessage] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    people: list[Person] = field(default_factory=list)
    # 不分群組的訊息範例：還沒設定任何群組時，讓使用者先看到訊息長什麼樣子
    samples: list[tuple[ServiceDay, OutgoingMessage]] = field(default_factory=list)

    def add(self, severity: Severity, code: str, message: str, hint: str = "") -> None:
        issue = Issue(severity, code, message, hint)
        if issue not in self.issues:
            self.issues.append(issue)

    @property
    def service_date(self) -> dt.date | None:
        return self.days[0].date if self.days else None


def active_targets(targets: list[Target]) -> list[Target]:
    return [t for t in targets if t.enabled and LINE_ID_RE.match(t.line_id)]


def find_unknown_names(roster: Roster, directory: Directory, since: dt.date) -> dict[str, tuple[str, ...]]:
    """整份服事表（從 since 開始）裡對不到同工名單的名字 → 猜測的人選。給「同工名單」頁用。"""
    unknown: dict[str, tuple[str, ...]] = {}
    for day in roster.days:
        if day.date < since:
            continue
        for name in day.all_names():
            if name not in unknown and not (p := directory.resolve(name)).matched:
                unknown[name] = p.suggestions
    return unknown


class Planner:
    def __init__(self, renderer: Renderer, directory: Directory, behavior: BehaviorSettings) -> None:
        self.renderer = renderer
        self.directory = directory
        self.behavior = behavior

    def plan(self, roster: Roster, targets: list[Target], today: dt.date) -> Plan:
        end = today + dt.timedelta(days=self.behavior.lookahead_days - 1)
        plan = Plan(today=today, window_end=end, issues=list(roster.issues))
        plan.days = [d for d in roster.days if today <= d.date <= end]

        self._check_coverage(plan, roster)
        if plan.days:
            plan.samples = [(d, self.renderer.render(d, self.directory)) for d in plan.days
                            if any(a.names for a in d.assignments)]
            self._build_messages(plan, targets)
            self._check_people(plan, targets)
            self._check_empty_roles(plan)
        return plan

    # ------------------------------------------------------------------ checks

    def _check_coverage(self, plan: Plan, roster: Roster) -> None:
        last = roster.last_date
        if last is None:
            return  # 解析階段已經報過「找不到日期」
        span = f"{format_date(plan.today, DATE_FMT)} ～ {format_date(plan.window_end, DATE_FMT)}"
        if not plan.days:
            if last < plan.today:
                plan.add(Severity.ERROR, "roster_expired",
                         f"服事表已經用完了（最後一天是 {format_date(last, DATE_FMT)}），這次沒有東西可以提醒",
                         "請把新的服事表加進 Google Sheet；如果換了新分頁，記得到「設定」更新網址。")
            else:
                plan.add(Severity.ERROR, "roster_gap", f"{span} 服事表裡沒有任何聚會，這次沒有東西可以提醒",
                         "如果這週真的沒有聚會可以忽略；否則請檢查服事表的日期有沒有寫錯。")
            return
        remaining = (last - plan.today).days
        if remaining < self.behavior.roster_low_warning_days:
            plan.add(Severity.WARNING, "roster_low",
                     f"服事表只排到 {format_date(last, DATE_FMT)}（剩 {remaining} 天），該排下一期了",
                     "排好後直接加在同一張表下面就好；如果開了新分頁，記得到「設定」更新網址。")

    def _build_messages(self, plan: Plan, targets: list[Target]) -> None:
        for target in active_targets(targets):
            got_any = False
            for day in plan.days:
                if target.labels and not matches_any(day.label, target.labels):
                    continue
                if not select_assignments(day, target):
                    continue
                message = self.renderer.render(day, self.directory, target)
                plan.messages.append(PlannedMessage(target, day, message, fingerprint(message.text)))
                got_any = True
            if not got_any:
                filters = "、".join([*target.roles, *target.labels])
                plan.add(Severity.WARNING, "target_nothing",
                         f"「{target.name}」這次沒有符合的服事內容，所以不會發",
                         f"LINE 群組設定只發「{filters}」，請確認跟服事表上的寫法一樣。" if filters
                         else "這幾天的服事表都是空白的。")

    def _check_people(self, plan: Plan, targets: list[Target]) -> None:
        mention_on = any(t.mention for t in active_targets(targets))
        seen: set[str] = set()
        for day in plan.days:
            when = format_date(day.date, DATE_FMT)
            for a in day.assignments:
                for raw in a.names:
                    person = self.directory.resolve(raw)
                    if raw not in seen:
                        seen.add(raw)
                        plan.people.append(person)
                    self._check_person(plan, person, when, a.role, mention_on)
                    for one in person.team_people:  # 小團裡的每一位也要檢查（停用、沒有 userId…）
                        self._check_person(plan, one, when, f"{a.role}・{person.display}", mention_on)
        if self.directory.is_empty and plan.people:
            plan.add(Severity.INFO, "no_members", "同工名單是空的，名字會照服事表原樣顯示",
                     "不需要 @ 人、名字也不用換的話，可以不設定同工名單。")

    def _check_person(self, plan: Plan, p: Person, when: str, role: str, mention_on: bool) -> None:
        if self.directory.is_empty:
            return
        if p.team is not None:
            if not p.team.active:
                plan.add(Severity.WARNING, "inactive_team",
                         f"小團「{p.team.name}」在小團名單是「停用」，但 {when} 被排了「{role}」",
                         "確認服事表是不是要換團；這一團還在服事的話，把小團名單的「啟用」改回「是」。")
            return  # 團裡的每一位由呼叫端逐一檢查
        if not p.matched:
            # 小團成員對不到，讀名單時（directory.validate_teams）已經整份報過一次，這裡不重複
            if self.behavior.warn_unknown_names and not p.via_team:
                guess = f"可能是：{'、'.join(p.suggestions)}？" if p.suggestions else ""
                plan.add(Severity.WARNING, "unknown_name", f"服事表上的「{p.raw}」在同工名單找不到（{when} {role}）",
                         f"{guess}到「同工名單」頁把它新增，或設成某人的「其他寫法」。訊息仍會照原樣送出。")
            return
        if p.member and not p.member.active:
            plan.add(Severity.WARNING, "inactive_member",
                     f"「{p.member.name}」在同工名單是「停用」，但 {when} 被排了「{role}」",
                     "確認服事表是不是要換人；如果他回來服事了，把同工名單的「啟用」改回「是」。")
        if mention_on and p.member and not p.member.line_user_id:
            plan.add(Severity.INFO, "no_user_id", f"「{p.member.name}」沒有 LINE_userId，訊息裡不會 @ 到他",
                     "請他在有機器人的群組打「/我的ID」，把回覆的 ID 填到同工名單。")

    def _check_empty_roles(self, plan: Plan) -> None:
        for day in plan.days:
            when = format_date(day.date, DATE_FMT)
            empty = [a.role for a in day.assignments if not a.names]
            if day.assignments and len(empty) == len(day.assignments):
                plan.add(Severity.WARNING, "day_empty", f"{when} {day.label} 服事表整天都是空白的",
                         "是不是還沒排？排好後下次發送就會帶到。")
            elif empty:
                plan.add(Severity.INFO, "roles_empty", f"{when} 沒排人的項目：{'、'.join(empty)}")
