"""在 LINE 聊天室用「/設定 名稱=值」修改機器人設定，每次都要一次性驗證碼。

安全設計（驗證碼要能擋住「群組裡隨便一個人打指令」）：
* 驗證碼只送到「別的管道」：執行機器人的電腦畫面，以及管理員的 LINE 私訊；打指令的人自己拿不到。
* 6 位數字、5 分鐘內有效、只能用一次、錯 3 次就作廢；只有「同一個人、在同一個聊天室」輸入才算。
* 同一時間只能有一個等待驗證的修改，避免有人連發請求洗版。
* 程式裡不存驗證碼本身，只存 HMAC-SHA256（金鑰每次啟動程式隨機產生），資料庫和記錄檔都不會有驗證碼。
* 用 LINE 私訊驗證碼每天最多 MAX_CODE_PUSHES_PER_DAY 次（私訊會扣額度），超過就只顯示在電腦畫面上，
  有人一直亂打指令也耗不掉每週提醒要用的額度。
* 只開放幾個「改錯也不危險」的設定（OPTIONS），LINE 金鑰、密碼、管理員 ID 永遠不能從聊天室改。
* 管理網頁可以直接「核准 / 拒絕」等待中的修改（能開管理網頁的人本來就能改所有設定）。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import logging
import math
import re
import secrets
import threading
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable

from church_bot.config import Paths, Settings, update_settings
from church_bot.errors import ChurchBotError

log = logging.getLogger(__name__)

CODE_TTL = dt.timedelta(minutes=5)
MAX_ATTEMPTS = 3
MAX_CODE_PUSHES_PER_DAY = 10
CODE_RE = re.compile(r"\d{6}")

ON_WORDS = {"開", "開啟", "打開", "啟用", "是", "要", "on", "true", "yes", "y", "1", "activate", "active", "enable",
            "enabled"}
OFF_WORDS = {"關", "關閉", "關掉", "停用", "否", "不要", "off", "false", "no", "n", "0", "deactivate", "inactive",
             "disable", "disabled"}


# --------------------------------------------------------------------------- options


def _norm_key(text: str) -> str:
    return re.sub(r"[\s_\-]+", "", unicodedata.normalize("NFKC", text or "")).lower()


def _parse_switch(text: str) -> bool:
    word = _norm_key(text)
    if word in ON_WORDS:
        return True
    if word in OFF_WORDS:
        return False
    raise ValueError("請填「開」或「關」")


def _show_switch(value: bool) -> str:
    return "開" if value else "關"


def _parse_weeks(text: str) -> int:
    try:
        weeks = int(_norm_key(text))
    except ValueError as exc:
        raise ValueError("請填 1～8 的數字") from exc
    if not 1 <= weeks <= 8:
        raise ValueError("請填 1～8 的數字")
    return weeks


def _set_collect_names(settings: Settings, value: bool) -> None:
    settings.chat.collect_names = value


def _set_schedule(settings: Settings, value: bool) -> None:
    settings.schedule.enabled = value


def _set_easter_egg(settings: Settings, value: bool) -> None:
    settings.chat.easter_egg = value


def _set_every_n_weeks(settings: Settings, weeks: int) -> None:
    settings.schedule.every_n_weeks = weeks
    # 跟網頁設定頁的提醒一樣：每 N 週發一次，就要往後看至少 N 週，不然中間那幾週的服事會漏掉
    settings.behavior.lookahead_days = max(settings.behavior.lookahead_days, weeks * 7)


@dataclass(frozen=True, slots=True)
class RemoteOption:
    key: str
    aliases: tuple[str, ...]
    hint: str
    parse: Callable[[str], Any]
    show: Callable[[Any], str]
    apply: Callable[[Settings, Any], None]
    read: Callable[[Settings], Any]
    reschedule: bool = False  # 改完要不要重新排程

    def current(self, settings: Settings) -> str:
        return self.show(self.read(settings))


OPTIONS: tuple[RemoteOption, ...] = (
    RemoteOption("收集名單", ("collect_member", "collect_members", "collect_names", "collect", "名字登記", "登記名字"),
                 "開 = 大家可以打「/我的名字」「/我的暱稱」登記；關 = 不開放登記",
                 _parse_switch, _show_switch, _set_collect_names, lambda s: s.chat.collect_names),
    RemoteOption("自動發送", ("schedule", "schedule_enabled", "auto_send"),
                 "開 = 每週自動提醒大家；關 = 暫停自動提醒（例如放假）",
                 _parse_switch, _show_switch, _set_schedule, lambda s: s.schedule.enabled, reschedule=True),
    RemoteOption("每幾週", ("every_n_weeks", "weeks", "頻率"),
                 "填 1～8：隔幾週提醒一次（機器人會自動多往後看幾天，中間幾週的服事不會漏掉）",
                 _parse_weeks, str, _set_every_n_weeks, lambda s: s.schedule.every_n_weeks, reschedule=True),
    RemoteOption("彩蛋", ("彩蛋模式", "easter_egg", "easteregg", "egg"),
                 "開 = 設定本來就是那個值時不糾正你，順著說「好，已經改好了」；關 = 照實說「不用改」",
                 _parse_switch, _show_switch, _set_easter_egg, lambda s: s.chat.easter_egg),
)


def find_option(key: str) -> RemoteOption | None:
    wanted = _norm_key(key)
    return next((o for o in OPTIONS if wanted in {_norm_key(k) for k in (o.key, *o.aliases)}), None)


def parse_assignment(arg: str) -> tuple[str, str]:
    """「收集名單=開」「COLLECT_MEMBER:true」「"收集名單":"開"」「收集名單 開」→ (名稱, 值)。"""
    text = re.sub(r"[\"'「」『』“”‘’]", "", unicodedata.normalize("NFKC", arg or "")).strip()
    match = re.match(r"^([^=:\s]+)\s*[=:\s]\s*(.+)$", text, re.DOTALL)
    if match is None:
        return text, ""
    return match[1], match[2].strip()


def describe_options(settings: Settings) -> str:
    minutes = int(CODE_TTL.total_seconds() // 60)
    lines = [f"⚙️ 用 LINE 可以改的設定（共 {len(OPTIONS)} 項）", ""]
    for option in OPTIONS:
        lines += [f"・{option.key}：現在是「{option.current(settings)}」", f"　{option.hint}"]
    lines += [
        "",
        "怎麼改（兩步）：",
        f"① 打「/設定 {OPTIONS[0].key}=開」",
        f"② 機器人會給管理員一組 6 位數驗證碼；請管理員告訴你，再打「/驗證 123456」（{minutes} 分鐘內有效）",
        "",
        "不想改了就打「/取消」。想知道誰能改什麼，打「/權限」。",
    ]
    return "\n".join(lines)


def apply_change(paths: Paths, change: "PendingChange") -> None:
    update_settings(paths, lambda settings: change.option.apply(settings, change.value))
    log.warning("設定已修改（%s）：%s%s → %s（提出的人：%s，%s）", change.approved_by,
                f"{change.ministry_name}・" if change.ministry_name else "", change.option.key, change.value_text,
                change.requester or change.user_id, change.chat_label)


# --------------------------------------------------------------------------- verification


class VerifyError(ChurchBotError):
    """驗證失敗；message 就是要回覆給使用者的話。"""


class NothingPending(VerifyError):
    pass


@dataclass(slots=True)
class PendingChange:
    option: RemoteOption
    value: Any
    value_text: str
    user_id: str
    chat_id: str
    requester: str
    chat_label: str
    expires_at: dt.datetime
    attempts_left: int
    digest: bytes = field(repr=False)
    approved_by: str = ""
    ministry_id: str = ""  # 改的是哪個牧區的設定（在哪個牧區的群組打的指令）
    ministry_name: str = ""

    def minutes_left(self, now: dt.datetime) -> int:
        return max(1, math.ceil((self.expires_at - now).total_seconds() / 60))


def _now() -> dt.datetime:
    return dt.datetime.now().astimezone()


class Verifier:
    """保管「等待驗證的設定修改」。整個程式共用一個（網頁和 Webhook 都看得到同一個）。"""

    def __init__(self, clock: Callable[[], dt.datetime] = _now) -> None:
        self._clock = clock
        self._key = secrets.token_bytes(32)
        self._lock = threading.Lock()
        self._pending: PendingChange | None = None
        self._pushes: list[dt.datetime] = []

    def _digest(self, code: str) -> bytes:
        return hmac.new(self._key, code.encode("ascii"), hashlib.sha256).digest()

    def _expire(self) -> None:
        if self._pending is not None and self._clock() >= self._pending.expires_at:
            log.info("設定修改「%s → %s」驗證碼過期，已取消", self._pending.option.key, self._pending.value_text)
            self._pending = None

    def current(self) -> PendingChange | None:
        with self._lock:
            self._expire()
            return self._pending

    def minutes_left(self, change: PendingChange) -> int:
        return change.minutes_left(self._clock())

    def waiting_for(self, user_id: str, chat_id: str) -> bool:
        pending = self.current()
        return pending is not None and (pending.user_id, pending.chat_id) == (user_id, chat_id)

    def start(self, option: RemoteOption, value: Any, *, user_id: str, chat_id: str, requester: str = "",
              chat_label: str = "", ministry_id: str = "", ministry_name: str = "") -> tuple[PendingChange, str]:
        with self._lock:
            self._expire()
            if (busy := self._pending) is not None:
                minutes = busy.minutes_left(self._clock())
                if (busy.user_id, busy.chat_id) == (user_id, chat_id):
                    raise VerifyError(f"你剛剛要改的「{busy.option.key}」還在等驗證碼（剩 {minutes} 分鐘）。"
                                      "請輸入驗證碼，或打 /取消。")
                raise VerifyError(f"現在有別人正在修改設定，請 {minutes} 分鐘後再試。")
            code = f"{secrets.randbelow(1_000_000):06d}"
            self._pending = PendingChange(
                option=option, value=value, value_text=option.show(value), user_id=user_id, chat_id=chat_id,
                requester=requester, chat_label=chat_label, expires_at=self._clock() + CODE_TTL,
                attempts_left=MAX_ATTEMPTS, digest=self._digest(code), ministry_id=ministry_id,
                ministry_name=ministry_name,
            )
            return self._pending, code

    def allow_push(self) -> bool:
        """這次可不可以用 LINE 私訊驗證碼（每天上限，見檔案開頭）。"""
        with self._lock:
            now = self._clock()
            self._pushes = [t for t in self._pushes if now - t < dt.timedelta(days=1)]
            if len(self._pushes) >= MAX_CODE_PUSHES_PER_DAY:
                return False
            self._pushes.append(now)
            return True

    def submit(self, user_id: str, chat_id: str, code: str) -> PendingChange:
        """驗證碼正確 → 回傳並清除等待中的修改。其他情況丟 VerifyError / NothingPending。"""
        with self._lock:
            self._expire()
            pending = self._pending
            if pending is None or (pending.user_id, pending.chat_id) != (user_id, chat_id):
                raise NothingPending("目前沒有等你驗證的設定修改（驗證碼 5 分鐘內有效，可能已經過期了）。")
            if hmac.compare_digest(pending.digest, self._digest(code)):
                self._pending = None
                pending.approved_by = "LINE 驗證碼"
                return pending
            pending.attempts_left -= 1
            if pending.attempts_left <= 0:
                self._pending = None
                log.warning("設定修改「%s」驗證碼錯誤 %d 次，已取消（%s）", pending.option.key, MAX_ATTEMPTS, user_id)
                raise VerifyError(f"驗證碼錯誤 {MAX_ATTEMPTS} 次，這次修改已經取消。")
            raise VerifyError(f"驗證碼不對，還可以再試 {pending.attempts_left} 次。")

    def cancel(self, user_id: str, chat_id: str) -> bool:
        with self._lock:
            if self._pending is not None and (self._pending.user_id, self._pending.chat_id) == (user_id, chat_id):
                self._pending = None
                return True
            return False

    def take(self, approved_by: str) -> PendingChange | None:
        """管理網頁按「核准」或「拒絕」：直接取出等待中的修改。"""
        with self._lock:
            self._expire()
            pending, self._pending = self._pending, None
            if pending is not None:
                pending.approved_by = approved_by
            return pending
