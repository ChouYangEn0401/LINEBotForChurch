"""主控台的「運作流程」與「需要處理的事」：服事表 → 同工名單 → LINE 群組 → 自動發送，
每一步現在的狀況、有問題要去哪一頁改。

讓不熟電腦的人一眼看懂「這幾頁是怎麼串起來的」，也知道紅色、黃色是卡在哪一步、按哪裡去處理。
"""

from __future__ import annotations

from dataclasses import dataclass

from church_bot.config import Settings
from church_bot.core.dates import format_date
from church_bot.models import Issue, Member, Roster, Severity, Target
from church_bot.tables import LINE_ID_RE

ROSTER_ERRORS = {"roster_expired", "roster_gap", "roster_no_dates", "SourceError", "SourceNotSetError", "sheet_error"}
ROSTER_WARNINGS = {"roster_low", "row_unreadable", "row_unreadable_more", "day_duplicate", "day_empty"}
MEMBER_WARNINGS = {"unknown_name", "inactive_member", "member_dup_name", "member_bad_uid"}
TARGET_ERRORS = {"no_active_target", "target_no_id", "target_bad_id", "table_error"}
TARGET_WARNINGS = {"target_nothing", "target_dup_id"}

# 問題代碼（或代碼開頭）→ (要去哪一頁, 按鈕上的字)。主控台「需要處理的事」每一項旁邊的「去處理」就是查這張表。
_ISSUE_LINKS: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("unknown_name", "inactive_member", "member_"), "/members", "到同工名單處理"),
    (("inactive_team", "team_"), "/members/teams", "到小團處理"),
    (("target_", "no_active_target"), "/targets", "到 LINE 群組處理"),
    (("SourceNotSetError",), "/roster#connect", "到服事表接上"),
    (("roster_", "row_unreadable", "SourceError", "sheet_error", "day_", "layout"), "/roster", "看服事表"),
    (("lookahead_too_short", "ConfigError", "table_error", "messenger_unavailable", "no_admin",
      "admin_alert_failed", "quota", "send_failed", "MessengerError"), "/settings", "到設定處理"),
)


def issue_link(code: str) -> tuple[str, str]:
    """回傳 (網址, 按鈕文字)；不認得的問題就回主控台自己（空網址 = 不顯示按鈕）。"""
    for prefixes, href, label in _ISSUE_LINKS:
        if any(code == p or code.startswith(p) for p in prefixes):
            return href, label
    return "", ""


@dataclass(frozen=True, slots=True)
class Step:
    icon: str
    title: str
    href: str
    status: str  # ok / warning / error / off（沒設定、但不是錯）
    summary: str
    detail: str = ""


def _worst(issues: list[Issue], errors: set[str], warnings: set[str]) -> str:
    codes = {i.code for i in issues if i.severity is not Severity.INFO}
    if codes & errors:
        return "error"
    if codes & warnings:
        return "warning"
    return "ok"


def build_steps(*, issues: list[Issue], settings: Settings, roster: Roster | None, roster_error: str,
                members: list[Member], targets: list[Target], unknown_names: int, pending_claims: int,
                next_run: str) -> list[Step]:
    steps: list[Step] = []

    if roster is None:
        steps.append(Step("📄", "服事表", "/roster", "error", "讀不到服事表", roster_error))
    else:
        last = format_date(roster.last_date, "%Y/%-m/%-d") if roster.last_date else "（沒有日期）"
        status = _worst(issues, ROSTER_ERRORS, ROSTER_WARNINGS)
        steps.append(Step("📄", "服事表", "/roster", status, f"排到 {last}", roster.source))

    parts = [f"{len(members)} 位同工"] if members else ["還沒設定（選用）"]
    if unknown_names:
        parts.append(f"{unknown_names} 個名字對不到")
    if pending_claims:
        parts.append(f"{pending_claims} 位 LINE 登記待確認")
    member_status = _worst(issues, set(), MEMBER_WARNINGS)
    if unknown_names or pending_claims:
        member_status = "warning"
    if not members and member_status == "ok":
        member_status = "off"
    steps.append(Step("🙋", "同工名單", "/members", member_status, "・".join(parts),
                      "服事表上的名字 ↔ 真實姓名 ↔ LINE 帳號"))

    active = [t for t in targets if t.enabled and LINE_ID_RE.match(t.line_id)]
    target_status = _worst(issues, TARGET_ERRORS, TARGET_WARNINGS)
    if not active:
        target_status = "error"
    steps.append(Step("👥", "LINE 群組", "/targets", target_status, f"{len(active)} 個會收到提醒",
                      f"共 {len(targets)} 個群組" if targets else "還沒有群組"))

    schedule = settings.schedule
    if settings.messenger.kind == "console":
        steps.append(Step("⏰", "自動發送", "/settings#advanced", "warning", "測試模式：不會真的送到 LINE",
                          schedule.describe()))
    elif not schedule.enabled:
        steps.append(Step("⏰", "自動發送", "/settings#schedule", "off", "自動發送關著",
                          "要按「立刻發送」，或由 Telegram 呼叫 cli.bat send --牧區"))
    else:
        steps.append(Step("⏰", "自動發送", "/settings#schedule", "ok", schedule.describe(), f"下次：{next_run}"))
    return steps
