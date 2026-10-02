"""本月 LINE 額度（用量）的快照：存在資料庫裡，主控台才會「永遠有數字可以看」。

為什麼要存起來，而不是每次想看就去問 LINE：
* 問一次要打兩支 API；主控台每次打開都問，網路慢的時候整頁跟著卡住。
* 問不到（沒網路、token 過期）時，主控台那一格以前會整個消失，看起來像壞掉了；
  存起來就能顯示「上次查到的數字」＋「那是什麼時候查的」，一眼看得出新不新。
* LINE 自己的用量統計會延遲幾分鐘：發送完馬上問，問到的數字通常還沒算進這一次。
  所以發送完要再排一次重查（``SETTLE``，見 ``BotService._note_push``）。

快照放在資料庫的 state 表（見 history.py）：管理網頁、``cli.bat send``、免費模式是三個不同的程式，
放資料庫誰更新的另一邊下次就看得到。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace

from church_bot.core.history import History
from church_bot.messengers.base import Quota

STATE_KEY = "line_quota"
FRESH = dt.timedelta(minutes=15)  # 快照比這個新就直接用，不去問 LINE
SETTLE = dt.timedelta(minutes=5)  # 發送完（或問不到時）過多久再問一次


def _parse_time(value: object) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _iso(when: dt.datetime | None) -> str:
    return when.isoformat(timespec="seconds") if when else ""


def _parse_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class QuotaSnapshot:
    """上次向 LINE 問到的用量。``checked_at`` 是 None = 從來沒問到過（畫面顯示「還沒查到」）。"""

    used: int = 0
    limit: int | None = None  # None = 無上限方案
    checked_at: dt.datetime | None = None
    next_try: dt.datetime | None = None  # 什麼時候該再問一次；None = 現在就該問
    pending: bool = False  # 剛發送完，LINE 的統計可能還沒算進這一次
    error: str = ""  # 上次問不到的原因（有數字也可能有這一項：代表數字是舊的）

    # ------------------------------------------------------------------ 看得到的內容

    @property
    def known(self) -> bool:
        return self.checked_at is not None

    @property
    def remaining(self) -> int | None:
        return None if self.limit is None else max(self.limit - self.used, 0)

    def describe(self) -> str:
        return Quota(limit=self.limit, used=self.used).describe() if self.known else "還沒查到本月用量"

    def age_text(self, now: dt.datetime) -> str:
        """「這個數字是什麼時候的」。看得出新不新，才知道它真的會更新。"""
        if self.checked_at is None:
            return "還沒查到"
        minutes = int((now - self.checked_at).total_seconds() // 60)
        if minutes < 1:
            return "剛剛更新"
        if minutes < 60:
            return f"{minutes} 分鐘前更新"
        if minutes < 60 * 24:
            return f"{minutes // 60} 小時前更新"
        return self.checked_at.strftime("%m/%d %H:%M") + " 更新"

    def status_text(self, now: dt.datetime) -> str:
        """畫面上那行小字：數字新不新、在等什麼、有沒有問不到。"""
        parts = [self.age_text(now)]
        if self.pending:
            parts.append("剛發送完，LINE 的統計再幾分鐘才會算進去")
        if self.error:
            parts.append(f"查不到最新：{self.error}")
        return "・".join(parts)

    # ------------------------------------------------------------------ 什麼時候該再問

    def due(self, now: dt.datetime) -> bool:
        return self.next_try is None or now >= self.next_try

    def updated(self, quota: Quota, now: dt.datetime) -> QuotaSnapshot:
        """問到了：換成新的數字，清掉「等 LINE 更新」和上次的錯誤。"""
        return QuotaSnapshot(used=quota.used, limit=quota.limit, checked_at=now, next_try=now + FRESH)

    def failed(self, reason: str, now: dt.datetime) -> QuotaSnapshot:
        """問不到：數字和時間都留著（舊的也比沒有好），記下原因，等一下再試。"""
        return replace(self, next_try=now + SETTLE, error=reason)

    def dirty(self, now: dt.datetime) -> QuotaSnapshot:
        """剛發送完：排一次 SETTLE 之後的重查，並標記現在這個數字可能還沒算進這一次。"""
        return replace(self, next_try=now + SETTLE, pending=True)

    # ------------------------------------------------------------------ 存進資料庫 / 讀回來

    def to_state(self) -> dict:
        return {"used": self.used, "limit": self.limit, "checked_at": _iso(self.checked_at),
                "next_try": _iso(self.next_try), "pending": self.pending, "error": self.error}

    @classmethod
    def from_state(cls, data: dict) -> QuotaSnapshot:
        return cls(
            used=_parse_int(data.get("used")) or 0,
            limit=_parse_int(data.get("limit")),
            checked_at=_parse_time(data.get("checked_at")),
            next_try=_parse_time(data.get("next_try")),
            pending=bool(data.get("pending")),
            error=str(data.get("error") or ""),
        )


def load_quota(history: History) -> QuotaSnapshot:
    return QuotaSnapshot.from_state(history.get_state(STATE_KEY))


def save_quota(history: History, snapshot: QuotaSnapshot) -> QuotaSnapshot:
    history.set_state(STATE_KEY, snapshot.to_state())
    return snapshot
