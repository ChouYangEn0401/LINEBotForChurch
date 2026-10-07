"""Telegram 一次性登入碼：伺服器管理員從外面登入時，管理者密碼之外的第二道（另一個選擇是驗證器 App，見 totp.py）。

照 CloudServicesPlatform（CSP）的做法：
* 按「傳碼到 Telegram」→ Telegram 收到 6 位數 → 在同一個頁面輸入。
* **碼只是一半**：另一半是發起登入的那個頁面自己拿著的 128-bit 隨機字串（nonce，放在那一頁的隱藏欄位）。
  攔截到 6 位數、偷看到手機螢幕的人，在自己的瀏覽器裡還是按不進來。
* 90 秒內有效、用過就失效；同時只有一筆（再按一次就換新的，舊的作廢）；按錯 3 次整筆作廢。
  拿錯 nonce 的人按錯不會用掉那 3 次（不然誰都能把你正在進行的登入弄掉）。
* 要碼有速率限制（10 分鐘 5 次），避免有人一直按讓你的手機響個不停。
* 碼只存在記憶體、只存雜湊；Telegram 那則訊息在碼用掉或過期時自動刪除。

設定（.env）：TELEGRAM_BOT_TOKEN（跟排程用的那個 Telegram 機器人同一個就可以）、SERVER_MANAGER_TELEGRAM_ID（你的 Telegram 數字 ID）。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import logging
import secrets
import threading
from dataclasses import dataclass
from typing import Callable, Protocol

import httpx

from church_bot.errors import ChurchBotError

log = logging.getLogger(__name__)

CODE_TTL = dt.timedelta(seconds=90)
MAX_ATTEMPTS = 3
RATE_WINDOW = dt.timedelta(minutes=10)
RATE_LIMIT = 5


class LoginCodeError(ChurchBotError):
    """登入碼不能用；message 就是要給使用者看的話（一律不透露現在有沒有別人在登入）。"""


class Sender(Protocol):
    def send(self, chat_id: str, html: str) -> int | None: ...
    def delete(self, chat_id: str, message_id: int) -> None: ...


class TelegramSender:
    """只用到 Telegram Bot API 的兩個方法：sendMessage、deleteMessage。"""

    def __init__(self, token: str, timeout: float = 10.0) -> None:
        self._base = f"https://api.telegram.org/bot{token}"
        self._timeout = timeout

    def send(self, chat_id: str, html: str) -> int | None:
        try:
            r = httpx.post(f"{self._base}/sendMessage", timeout=self._timeout,
                           json={"chat_id": chat_id, "text": html, "parse_mode": "HTML"})
            data = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LoginCodeError("連不上 Telegram，這次沒辦法傳登入碼", "改用驗證器 App 的 6 位數，或等一下再試。") from exc
        if not data.get("ok"):
            log.error("Telegram 拒絕傳登入碼：%s", data.get("description"))  # 不印 token
            raise LoginCodeError("Telegram 沒有傳出登入碼", "確認 .env 的 TELEGRAM_BOT_TOKEN、SERVER_MANAGER_TELEGRAM_ID，"
                                                         "而且你已經先跟那個 Telegram 機器人說過話。")
        return (data.get("result") or {}).get("message_id")

    def delete(self, chat_id: str, message_id: int) -> None:
        try:
            httpx.post(f"{self._base}/deleteMessage", timeout=self._timeout,
                       json={"chat_id": chat_id, "message_id": message_id})
        except httpx.HTTPError:
            pass  # 刪不掉就算了：碼本來就已經失效


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(slots=True)
class _Pending:
    nonce_hash: str
    code_hash: str
    expires_at: dt.datetime
    attempts_left: int
    chat_id: str
    message_id: int | None


class LoginCodes:
    def __init__(self, clock: Callable[[], dt.datetime] | None = None) -> None:
        self._clock = clock or (lambda: dt.datetime.now().astimezone())
        self._lock = threading.Lock()
        self._pending: _Pending | None = None
        self._requests: list[dt.datetime] = []
        self._sender: Sender | None = None

    def start(self, sender: Sender, chat_id: str) -> str:
        """傳一組新的碼到 Telegram，回傳 nonce（放在發起登入的那一頁）。舊的那一筆當場作廢。"""
        now = self._clock()
        with self._lock:
            self._requests = [t for t in self._requests if now - t < RATE_WINDOW]
            if len(self._requests) >= RATE_LIMIT:
                raise LoginCodeError("要登入碼的次數太多了，請 10 分鐘後再試", "或改用驗證器 App 的 6 位數。")
            self._requests.append(now)
            self._discard()
            code = f"{secrets.randbelow(1_000_000):06d}"
            nonce = secrets.token_urlsafe(16)
        text = (f"🔐 服事提醒機器人：伺服器管理員登入碼 <tg-spoiler>{code}</tg-spoiler>\n"
                f"{int(CODE_TTL.total_seconds())} 秒內有效、只能用一次。不是你在登入的話不用理它。")
        message_id = sender.send(chat_id, text)
        with self._lock:
            self._sender = sender
            self._pending = _Pending(_digest(nonce), _digest(code), now + CODE_TTL, MAX_ATTEMPTS, chat_id, message_id)
        timer = threading.Timer(CODE_TTL.total_seconds() + 1, self._expire_check)
        timer.daemon = True
        timer.start()
        log.info("已傳伺服器管理員登入碼到 Telegram")
        return nonce

    def verify(self, nonce: str, code: str) -> bool:
        code = "".join(ch for ch in code if ch.isdigit())
        with self._lock:
            pending = self._pending
            if pending is None or self._clock() >= pending.expires_at:
                self._discard()
                return False
            if not hmac.compare_digest(_digest(nonce or ""), pending.nonce_hash):
                return False  # 不是發起登入的那一頁：不扣那 3 次
            if hmac.compare_digest(_digest(code), pending.code_hash):
                self._discard()
                return True
            pending.attempts_left -= 1
            if pending.attempts_left <= 0:
                self._discard()
            return False

    def _expire_check(self) -> None:
        with self._lock:
            if self._pending is not None and self._clock() >= self._pending.expires_at:
                self._discard()

    def _discard(self) -> None:
        """作廢目前那一筆，並把 Telegram 上那則訊息刪掉（呼叫的人要拿著 _lock）。"""
        pending, self._pending = self._pending, None
        if pending is not None and pending.message_id is not None and self._sender is not None:
            sender, chat_id, message_id = self._sender, pending.chat_id, pending.message_id
            threading.Thread(target=sender.delete, args=(chat_id, message_id), daemon=True).start()
