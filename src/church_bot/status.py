"""「現在整體狀況怎麼樣」：每個牧區的服事表排到哪、上一次發了什麼、下一次什麼時候發。

同一份資料給三個地方用，所以集中在這裡算一次：

* ``cli.bat 狀態`` —— 伺服器管理員在這台電腦（或 Telegram 呼叫）一個指令看全部（``cli.cmd_status``）
* 後台啟動時傳到 Telegram 的那一則（``core/notify.py`` 負責傳，內容從這裡來）
* ``cli.bat 狀態 --telegram`` —— 人在外面時直接把同一份傳到手機

服事表要連網去讀（Google），所以慢；``check_roster=False`` 就只看排程和發送紀錄，瞬間就好。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from church_bot import __version__
from church_bot.config import load_settings
from church_bot.core.dates import format_date
from church_bot.core.history import RunSummary
from church_bot.errors import ChurchBotError
from church_bot.ministries import Church, Unit
from church_bot.scheduler import format_when, next_fire_time

SENT_TRIGGERS = ("schedule", "catchup", "manual", "cli")  # 真的 Push 出去的那幾種（/提醒 的 reply 不算）


@dataclass(frozen=True, slots=True)
class MinistryStatus:
    """一個牧區現在的樣子。``ok=False`` = 有東西要處理（指令的結束代碼會是 1）。"""

    id: str
    name: str
    schedule: str  # 「每星期四 20:00」「自動發送已關閉」
    next_run: dt.datetime | None
    last: RunSummary | None
    roster: str  # 「排到 2026/11/30」「讀不到：…」「（沒有查）」
    ok: bool

    @property
    def next_text(self) -> str:
        return format_when(self.next_run) if self.next_run else "（不會自動發）"

    @property
    def last_text(self) -> str:
        if self.last is None:
            return "（還沒發過）"
        when = self.last.started_at[:16].replace("T", " ")
        counts = f"送出 {self.last.sent}"
        if self.last.failed:
            counts += f"、失敗 {self.last.failed}"
        if self.last.skipped:
            counts += f"、略過 {self.last.skipped}"
        return f"{when}　{self.last.status_zh}　{counts}　（{self.last.trigger_zh}）"


def collect(church: Church, *, check_roster: bool = True, now: dt.datetime | None = None) -> list[MinistryStatus]:
    return [_one(unit, check_roster=check_roster, now=now) for unit in church.units()]


def _one(unit: Unit, *, check_roster: bool, now: dt.datetime | None) -> MinistryStatus:
    service = unit.service
    last = service.history.last_run(SENT_TRIGGERS)
    try:
        settings = load_settings(service.paths)
    except ChurchBotError as exc:
        return MinistryStatus(unit.id, unit.name, f"設定檔有誤：{exc.message}", None, last, "（設定檔有誤，沒有查）",
                              ok=False)

    cfg = settings.schedule
    moment = now or dt.datetime.now(ZoneInfo(cfg.timezone))
    upcoming = next_fire_time(cfg, moment) if cfg.enabled else None

    roster_text, roster_ok = "（沒有查）", True
    if check_roster:
        try:
            roster = service.fetch_roster(settings, moment.date(), use_cache=True)
        except ChurchBotError as exc:
            roster_text, roster_ok = f"讀不到：{exc.message}", False
        else:
            last_date = format_date(roster.last_date, "%Y/%-m/%-d") if roster.last_date else "（沒有日期）"
            left = (roster.last_date - moment.date()).days if roster.last_date else -1
            roster_text = f"排到 {last_date}（剩 {left} 天）" if left >= 0 else f"排到 {last_date}　⚠️ 已經排完了"
            roster_ok = left >= settings.behavior.roster_low_warning_days

    ok = roster_ok and not (last is not None and last.status == "error")
    return MinistryStatus(unit.id, unit.name, cfg.describe(), upcoming, last, roster_text, ok)


def lines(statuses: list[MinistryStatus], *, detail: bool = True) -> list[str]:
    """給人看的幾行字。``detail=False`` = 一個牧區一行（啟動通知用，不要太長）。"""
    if not statuses:
        return ["（還沒有任何牧區）"]
    out: list[str] = []
    for s in statuses:
        if not detail:
            out.append(f"{'✅' if s.ok else '⚠️'} {s.name}：下次 {s.next_text}")
            continue
        out += [
            f"{'✅' if s.ok else '⚠️'} {s.name}（{s.id}）",
            f"　服事表：{s.roster}",
            f"　上一次發送：{s.last_text}",
            f"　下一次發送：{s.next_text}　{s.schedule}",
            "",
        ]
    return out


def text(church: Church, statuses: list[MinistryStatus], *, detail: bool = True, title: str = "") -> str:
    head = title or f"📋 服事提醒機器人 v{__version__}：各牧區狀況"
    out = [head, f"（{dt.datetime.now().astimezone():%Y/%m/%d %H:%M}）", ""]
    out += lines(statuses, detail=detail)
    quota = church.quota_status()
    if quota is not None and quota.describe():
        out.append(f"LINE 本月用量：{quota.describe()}")
    return "\n".join(out).rstrip()
