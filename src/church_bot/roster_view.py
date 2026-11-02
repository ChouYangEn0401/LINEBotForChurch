"""「看服事表」：以今天為基準上兩週＋下四週，每一天誰服事什麼、穿什麼、哪裡被調換過。

給 ``roster show``（JSON）和 ``roster image``（畫成一張 PNG，見 roster_image.py）用。
資料跟 ``/提醒``、``/行事曆`` 走同一條路：``BotService.fetch_raw_roster`` 讀 Sheet，
再疊人員調換（core/overrides.py，就是 ``fetch_roster`` 做的那兩步；這裡拆開只是為了順便拿到每一筆調換有沒有套上）。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from church_bot.core import calendar, overrides
from church_bot.core.directory import Directory
from church_bot.models import ServiceDay
from church_bot.service import BotService

MAX_WEEKS_BACK = 12
MAX_WEEKS_FORWARD = 26


@dataclass(frozen=True, slots=True)
class Slot:
    role: str
    names: tuple[str, ...]  # 訊息上顯示的名字（對過同工名單；小團是團名）
    raw: tuple[str, ...]  # 服事表上的寫法
    adjusted: bool


@dataclass(frozen=True, slots=True)
class DayView:
    date: dt.date
    label: str
    note: str
    texts: tuple[tuple[str, str], ...]
    slots: tuple[Slot, ...]
    adjusted: bool
    adjusted_texts: tuple[str, ...]
    past: bool
    this_week: bool

    @property
    def weekday(self) -> str:
        return calendar.weekday_zh(self.date)

    @property
    def when(self) -> str:
        return f"{self.date.month}/{self.date.day}（{self.weekday}）"


@dataclass(slots=True)
class RosterView:
    today: dt.date
    start: dt.date
    end: dt.date
    back: int
    forward: int
    column: str  # 「照原樣顯示的欄位」裡填得最多的那一欄（/行事曆 用的那一欄），沒有就空字串
    source: str
    days: list[DayView] = field(default_factory=list)
    overrides_active: int = 0
    overrides_applied: int = 0

    @property
    def roles(self) -> list[str]:
        """這段時間出現過的服事項目（照服事表的順序），畫表格的欄用。"""
        seen: dict[str, None] = {}
        for day in self.days:
            for slot in day.slots:
                seen.setdefault(slot.role, None)
        return list(seen)

    def to_json(self) -> dict:
        return {
            "today": self.today.isoformat(),
            "window": {"start": self.start.isoformat(), "end": self.end.isoformat(),
                       "weeks_back": self.back, "weeks_forward": self.forward},
            "column": self.column,
            "source": self.source,
            "overrides": {"active": self.overrides_active, "applied": self.overrides_applied},
            "days": [{
                "date": d.date.isoformat(), "weekday": d.weekday, "label": d.label, "note": d.note,
                "texts": [[k, v] for k, v in d.texts],
                "assignments": [{"role": s.role, "names": list(s.names), "raw": list(s.raw), "adjusted": s.adjusted}
                                for s in d.slots],
                "adjusted": d.adjusted, "adjusted_texts": list(d.adjusted_texts),
                "past": d.past, "this_week": d.this_week,
            } for d in self.days],
        }


def _day_view(day: ServiceDay, directory: Directory, today: dt.date) -> DayView:
    this_week = calendar.week_start(today)
    slots = tuple(Slot(role=a.role, names=tuple(directory.resolve(n).display for n in a.names), raw=a.names,
                       adjusted=day.adjusted_on(a.role)) for a in day.assignments)
    return DayView(date=day.date, label=day.label, note=day.note, texts=tuple((k, v) for k, v in day.texts if v),
                   slots=slots, adjusted=day.is_adjusted,
                   adjusted_texts=tuple(k for k, _ in day.texts if day.adjusted_on(k)),
                   past=day.date < today, this_week=this_week <= day.date < this_week + dt.timedelta(days=7))


def collect(service: BotService, back: int = calendar.WEEKS_BACK, forward: int = calendar.WEEKS_FORWARD,
            *, use_cache: bool = False) -> RosterView:
    """讀服事表（疊完調換）→ 視窗內的每一天。讀不到服事表丟 ChurchBotError。"""
    back = max(0, min(back, MAX_WEEKS_BACK))
    forward = max(1, min(forward, MAX_WEEKS_FORWARD))
    ctx = service.load()
    today = service.now(ctx.settings).date()
    raw = service.fetch_raw_roster(ctx.settings, today, use_cache=use_cache)
    roster, states = overrides.overlay(service.paths, raw)
    start, end = calendar.window(today, back, forward)
    directory = ctx.directory
    days = [_day_view(d, directory, today) for d in sorted(roster.days, key=lambda d: d.date) if start <= d.date <= end]
    return RosterView(today=today, start=start, end=end, back=back, forward=forward,
                      column=calendar.resolve_column(roster.days), source=roster.source, days=days,
                      overrides_active=len(states) - sum(1 for s in states.values() if s == "retired"),
                      overrides_applied=sum(1 for s in states.values() if s == "applied"))


def as_text(view: RosterView, ministry: str = "") -> list[str]:
    """給人看的幾行字（沒加 --json 時）。"""
    lines = [f"📅 {ministry + ' ' if ministry else ''}服事表 {view.start.month}/{view.start.day} ～ "
             f"{view.end.month}/{view.end.day}（今天 {view.today.month}/{view.today.day}）"]
    if not view.days:
        lines.append("（這段時間服事表上沒有聚會）")
    for d in view.days:
        mark = "👉 " if d.this_week else "　 "
        head = f"{mark}{d.when}{' ' + d.label if d.label else ''}{'　🔁 已調整' if d.adjusted else ''}"
        body = "　".join(f"{s.role}：{'、'.join(s.names) or '—'}{'🔁' if s.adjusted else ''}" for s in d.slots)
        texts = "　".join(f"{k}：{v}{'🔁' if k in d.adjusted_texts else ''}" for k, v in d.texts)
        lines += [head, f"　　{body}"] + ([f"　　{texts}"] if texts else [])
    lines.append(f"（資料來源：{view.source}；人員調換 {view.overrides_applied} 筆套用中）")
    return lines
