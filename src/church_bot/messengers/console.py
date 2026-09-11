"""測試用發送端：不連 LINE，把訊息寫到 data/outbox.log。

設定 ``messenger.kind: console`` 就會用它 —— 可以在還沒申請 LINE 之前先把整套流程跑一遍。
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

from church_bot.messengers.base import Quota, SendResult
from church_bot.models import OutgoingMessage

log = logging.getLogger(__name__)


class ConsoleMessenger:
    name = "測試模式（不會真的送到 LINE）"

    def __init__(self, outbox: Path) -> None:
        self.outbox = outbox

    def send(self, to: str, message: OutgoingMessage) -> SendResult:
        self.outbox.parent.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().isoformat(timespec="seconds")
        with self.outbox.open("a", encoding="utf-8") as fh:
            fh.write(f"===== {stamp} → {to}\n{message.text}\n\n")
        log.info("[測試模式] 訊息寫入 %s（收件：%s）", self.outbox.name, to)
        return SendResult(note=f"測試模式：訊息寫到 {self.outbox.name}，沒有真的送出", warn=False)

    def check(self) -> str:
        return self.name

    def audience_size(self, to: str) -> int | None:
        return None

    def quota(self) -> Quota | None:
        return None

    def close(self) -> None:
        pass
