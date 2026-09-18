"""Domain models — 純資料，不做任何 I/O。"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field, replace
from enum import StrEnum


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Issue:
    """一個需要讓人知道的狀況。ERROR = 這次提醒可能沒送出或內容錯誤。"""

    severity: Severity
    code: str
    message: str
    hint: str = ""

    @property
    def is_error(self) -> bool:
        return self.severity is Severity.ERROR

    @property
    def icon(self) -> str:
        return {"info": "ℹ️", "warning": "⚠️", "error": "❌"}[self.severity.value]

    def one_line(self) -> str:
        text = f"{self.icon} {self.message}"
        return f"{text}\n   → {self.hint}" if self.hint else text


# --------------------------------------------------------------------------- roster


@dataclass(frozen=True, slots=True)
class Assignment:
    """某一項服事（例如「司琴」）由哪些人負責。"""

    role: str
    names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ServiceDay:
    """某一天（某一場聚會）的完整服事安排。"""

    date: dt.date
    assignments: tuple[Assignment, ...]
    label: str = ""  # 例如「主日崇拜」「青年崇拜」；表格沒有就空字串
    note: str = ""

    def all_names(self) -> list[str]:
        seen: dict[str, None] = {}
        for a in self.assignments:
            for n in a.names:
                seen.setdefault(n, None)
        return list(seen)


@dataclass(frozen=True, slots=True)
class Roster:
    days: tuple[ServiceDay, ...]
    source: str  # 給人看的來源描述，例如「Google Sheet：2026 服事表 / 9月」
    issues: tuple[Issue, ...] = ()

    @property
    def last_date(self) -> dt.date | None:
        return max((d.date for d in self.days), default=None)

    def all_names(self) -> set[str]:
        return {n for d in self.days for n in d.all_names()}

    def all_roles(self) -> list[str]:
        seen: dict[str, None] = {}
        for d in self.days:
            for a in d.assignments:
                seen.setdefault(a.role, None)
        return list(seen)


# --------------------------------------------------------------------------- mapping tables


@dataclass(frozen=True, slots=True)
class Target:
    """要收到提醒的 LINE 群組（或個人）。對應 config/targets.csv 的一列。"""

    name: str
    line_id: str
    enabled: bool = True
    roles: tuple[str, ...] = ()  # 只發這些服事項目；空 = 全部
    labels: tuple[str, ...] = ()  # 只發這些聚會；空 = 全部
    mention: bool = False  # 要不要在訊息裡 @ 服事的人
    note: str = ""


@dataclass(frozen=True, slots=True)
class Member:
    """一位同工。對應 config/members.csv 的一列。"""

    name: str  # 訊息上要顯示的名字
    aliases: tuple[str, ...] = ()  # 服事表上可能出現的其他寫法（綽號、英文名…）
    line_user_id: str = ""  # 要 @ 他時才需要
    active: bool = True
    note: str = ""


@dataclass(frozen=True, slots=True)
class Person:
    """服事表上的一個名字，對照完同工名單後的結果。"""

    raw: str  # 服事表上原本的寫法
    display: str  # 訊息上顯示的名字
    member: Member | None = None
    suggestions: tuple[str, ...] = ()  # 對不到時，猜他可能是誰

    @property
    def matched(self) -> bool:
        return self.member is not None


# --------------------------------------------------------------------------- run results


class DeliveryStatus(StrEnum):
    SENT = "sent"
    DRY_RUN = "dry_run"
    SKIPPED = "skipped"
    FAILED = "failed"

    @property
    def zh(self) -> str:
        return {
            "sent": "已送出",
            "dry_run": "預覽（沒有真的送）",
            "skipped": "略過",
            "failed": "失敗",
        }[self.value]


@dataclass(slots=True)
class Delivery:
    target_name: str
    target_id: str
    service_date: dt.date | None
    text: str
    status: DeliveryStatus
    detail: str = ""
    label: str = ""  # 聚會名稱（同一天有兩場聚會時用來區分）
    fingerprint: str = ""  # 訊息內容指紋，用來判斷服事表有沒有改過


@dataclass(slots=True)
class RunReport:
    """一次「檢查 + 發送」的完整結果。UI、紀錄、管理員通知都吃這個。"""

    run_id: str
    trigger: str  # schedule / manual / cli / preview
    dry_run: bool
    started_at: dt.datetime
    finished_at: dt.datetime | None = None
    service_date: dt.date | None = None
    deliveries: list[Delivery] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(i.is_error for i in self.issues) or any(
            d.status is DeliveryStatus.FAILED for d in self.deliveries
        )

    @property
    def has_warnings(self) -> bool:
        return any(i.severity is Severity.WARNING for i in self.issues)

    @property
    def status(self) -> str:
        if self.has_errors:
            return "error"
        if self.has_warnings:
            return "warning"
        return "ok"

    @property
    def status_zh(self) -> str:
        return {"error": "有錯誤", "warning": "有提醒事項", "ok": "正常"}[self.status]

    def count(self, status: DeliveryStatus) -> int:
        return sum(1 for d in self.deliveries if d.status is status)

    @property
    def count_sent(self) -> int:
        return self.count(DeliveryStatus.SENT)

    @property
    def count_failed(self) -> int:
        return self.count(DeliveryStatus.FAILED)

    @property
    def count_skipped(self) -> int:
        return self.count(DeliveryStatus.SKIPPED)

    @property
    def count_dry_run(self) -> int:
        return self.count(DeliveryStatus.DRY_RUN)


@dataclass(frozen=True, slots=True)
class RawSheet:
    """資料來源讀回來的原始格子（全部是字串）。解析交給 core.parser。"""

    rows: list[list[str]]
    source: str  # 給人看的來源描述


@dataclass(frozen=True, slots=True)
class OutgoingMessage:
    """要送出的一則訊息。text 一定有；有要 @ 人時另外帶 mention_text + mentions（LINE textV2 格式）。"""

    text: str
    mention_text: str = ""
    mentions: tuple[tuple[str, str], ...] = ()  # (佔位符 key, LINE userId)

    @property
    def has_mentions(self) -> bool:
        return bool(self.mentions and self.mention_text)


def tagged(message: OutgoingMessage, tag: str) -> OutgoingMessage:
    """在訊息最前面加一行標籤（例如標示這則是自動 Push 還是手動 Reply 送出的）。

    只能用在「實際送出／顯示」的那份文字上：fingerprint 是用規劃階段、還沒加標籤的原始內容算的，
    這樣同一週的內容不管最後走哪條管道送出，都會算成同一個 fingerprint，防重複發送才不會失準。
    """
    return replace(message, text=f"{tag}\n{message.text}",
                   mention_text=f"{tag}\n{message.mention_text}" if message.mention_text else "")


# --------------------------------------------------------------------------- run triggers

TRIGGER_ZH = {
    "schedule": "自動排程（Push，計費）",
    "manual": "網頁手動（Push，計費）",
    "cli": "指令（Push，計費）",
    "catchup": "開機補發（Push，計費）",
    "reply": "LINE 回覆（免費）",
    "preview": "預覽",
}
