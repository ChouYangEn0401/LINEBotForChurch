"""發送端介面。

想改成 Telegram、Email、Slack…只要實作 ``Messenger``，其他程式都不用改。
失敗一律丟 ``MessengerError``（帶中文 hint），不要默默吞掉。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from church_bot.models import OutgoingMessage


@dataclass(frozen=True, slots=True)
class SendResult:
    note: str = ""  # 例如「@ 標記失敗，已改用純文字送出」
    warn: bool = True  # True = 記成「提醒事項」（會通知管理員）；False = 只當參考資訊


@dataclass(frozen=True, slots=True)
class Quota:
    limit: int | None  # None = 沒有上限（付費方案可加購）
    used: int

    @property
    def remaining(self) -> int | None:
        return None if self.limit is None else max(self.limit - self.used, 0)

    def describe(self) -> str:
        if self.limit is None:
            return f"本月已用 {self.used} 則（無上限方案）"
        return f"本月已用 {self.used} / {self.limit} 則（剩 {self.remaining} 則）"


@runtime_checkable
class Messenger(Protocol):
    name: str

    def send(self, to: str, message: OutgoingMessage) -> SendResult: ...

    def check(self) -> str:
        """確認金鑰可用，回傳機器人名稱。"""
        ...

    def audience_size(self, to: str) -> int | None:
        """送一則到這裡會被算成幾則（群組 = 人數）。不知道就回 None。"""
        ...

    def quota(self) -> Quota | None: ...

    def close(self) -> None: ...
