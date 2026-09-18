"""LINE Messaging API。

直接用 httpx 呼叫 REST API，不依賴 line-bot-sdk：少一個大型套件，錯誤訊息也能完全自己翻成白話。
參考：https://developers.line.biz/en/reference/messaging-api/

重點行為：
* 每則訊息帶 X-Line-Retry-Key：網路斷線重試時 LINE 不會重複發送（重複會回 409 = 其實已送出）。
* 網路錯誤、5xx、限流才重試；token 錯、額度用完這種重試也沒用的錯誤直接回報。
* 有 @ 人的訊息（textV2）若被 LINE 拒絕，自動改送純文字，提醒不會因為 @ 失敗而整則沒送。
"""

from __future__ import annotations

import time
import uuid
from typing import Callable

import httpx

from church_bot.errors import ConfigError, MessengerError
from church_bot.messengers.base import Quota, SendResult
from church_bot.models import OutgoingMessage

API_BASE = "https://api.line.me"
TOKEN_HINT = (
    "到 LINE Developers → 你的 Channel →「Messaging API」分頁最下面，按 Issue 重新發行 Channel access token，"
    "再貼到網頁「設定 → LINE 金鑰」。"
)
QUOTA_HINT = (
    "免費方案每月 200 則，推播到群組是「按群組人數」計算。"
    "可到 LINE 官方帳號後台升級方案，或等下個月額度重置。詳見 docs/LINE_PRICING.md。"
)


def _error_detail(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:200]
    message = str(data.get("message", ""))
    details = "；".join(
        f"{d.get('property', '')} {d.get('message', '')}".strip() for d in data.get("details", []) or []
    )
    return f"{message}（{details}）" if details else message


class LineMessenger:
    name = "LINE"

    def __init__(
        self,
        token: str,
        timeout: float = 15.0,
        *,
        client: httpx.Client | None = None,
        max_attempts: int = 3,
        backoff: tuple[float, ...] = (2.0, 5.0),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not token:
            raise ConfigError("還沒設定 LINE Channel access token", TOKEN_HINT)
        self._client = client or httpx.Client(base_url=API_BASE, timeout=timeout)
        self._headers = {"Authorization": f"Bearer {token}"}
        self._max_attempts = max_attempts
        self._backoff = backoff
        self._sleep = sleep

    # ------------------------------------------------------------------ sending

    def send(self, to: str, message: OutgoingMessage) -> SendResult:
        plain = {"type": "text", "text": message.text}
        if not message.has_mentions:
            self._push(to, [plain])
            return SendResult()
        try:
            self._push(to, [self._text_v2(message)])
            return SendResult()
        except MessengerError as exc:
            if exc.status_code != 400:
                raise
            self._push(to, [plain])
            return SendResult(note=f"@ 標記被 LINE 拒絕，已改用純文字送出（{exc.message}）")

    def reply(self, reply_token: str, text: str) -> None:
        """回覆使用者剛傳的訊息（Reply API 不計入每月額度）。"""
        self.reply_texts(reply_token, [text])

    def reply_texts(self, reply_token: str, texts: list[str]) -> None:
        """一次回覆多則純文字泡泡（Reply API 不計入每月額度）。

        一次最多送 5 則（LINE 限制），且同一個 reply_token 只能用這一次，不能像 push 一樣重複呼叫重送。
        """
        messages = [{"type": "text", "text": t[:5000]} for t in texts[:5]]
        self._request("POST", "/v2/bot/message/reply", json={"replyToken": reply_token, "messages": messages})

    @staticmethod
    def _text_v2(message: OutgoingMessage) -> dict:
        return {
            "type": "textV2",
            "text": message.mention_text,
            "substitution": {
                key: {"type": "mention", "mentionee": {"type": "user", "userId": uid}}
                for key, uid in message.mentions
            },
        }

    def _push(self, to: str, messages: list[dict]) -> None:
        # 同一則訊息的每次重試都用同一個 retry key，LINE 保證不會重複發送
        self._request("POST", "/v2/bot/message/push", json={"to": to, "messages": messages},
                      headers={"X-Line-Retry-Key": str(uuid.uuid4())}, ok_statuses=(200, 409))

    # ------------------------------------------------------------------ info

    def check(self) -> str:
        info = self._request("GET", "/v2/bot/info").json()
        basic = f"（{info['basicId']}）" if info.get("basicId") else ""
        return f"{info.get('displayName', '?')}{basic}"

    def quota(self) -> Quota:
        limit = self._request("GET", "/v2/bot/message/quota").json()
        used = self._request("GET", "/v2/bot/message/quota/consumption").json()
        return Quota(
            limit=int(limit["value"]) if limit.get("type") == "limited" else None,
            used=int(used.get("totalUsage", 0)),
        )

    def audience_size(self, to: str) -> int | None:
        if to.startswith("U"):
            return 1
        kind = {"C": "group", "R": "room"}.get(to[:1])
        if kind is None:
            return None
        return int(self._request("GET", f"/v2/bot/{kind}/{to}/members/count").json().get("count", 0))

    def group_name(self, group_id: str) -> str:
        try:
            return str(self._request("GET", f"/v2/bot/group/{group_id}/summary").json().get("groupName", ""))
        except MessengerError:
            return ""

    def member_profile(self, chat_id: str, user_id: str) -> str:
        """某人的顯示名稱。群組/多人聊天室成員不用是好友也查得到；1 對 1 私訊要對方是好友（或 7 天內私訊過）。"""
        kind = {"C": "group", "R": "room"}.get(chat_id[:1])
        path = f"/v2/bot/{kind}/{chat_id}/member/{user_id}" if kind else f"/v2/bot/profile/{user_id}"
        try:
            return str(self._request("GET", path).json().get("displayName", ""))
        except MessengerError:
            return ""

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------ http

    def _request(self, method: str, path: str, *, json: dict | None = None, headers: dict | None = None,
                 ok_statuses: tuple[int, ...] = (200,)) -> httpx.Response:
        for attempt in range(1, self._max_attempts + 1):
            try:
                resp = self._client.request(method, path, json=json, headers={**self._headers, **(headers or {})})
            except httpx.TimeoutException:
                error = MessengerError("連 LINE 逾時", "檢查網路，程式會自動重試。", retryable=True)
            except httpx.TransportError as exc:
                error = MessengerError(f"連不上 LINE：{exc.__class__.__name__}", "檢查這台電腦的網路。", retryable=True)
            else:
                if resp.status_code in ok_statuses:
                    return resp
                error = self._to_error(resp)
            if not error.retryable or attempt == self._max_attempts:
                raise error
            self._sleep(self._backoff[min(attempt - 1, len(self._backoff) - 1)])
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _to_error(resp: httpx.Response) -> MessengerError:
        status = resp.status_code
        detail = _error_detail(resp)
        if status == 401:
            return MessengerError("LINE token 錯誤或已失效", TOKEN_HINT, status_code=status)
        if status == 403:
            return MessengerError(f"LINE 拒絕這個操作（沒有權限）：{detail}",
                                  "確認機器人還在群組裡、沒有被踢出；個人的話對方要先加機器人好友。", status_code=status)
        if status == 400:
            hint = ("LINE_ID 可能填錯，或機器人已經不在那個群組。" if "'to'" in detail or "to" == detail
                    else "如果是自己改過訊息模板，請按「恢復預設模板」。")
            return MessengerError(f"LINE 不接受這則訊息：{detail}", hint, status_code=status)
        if status == 404:
            return MessengerError(f"LINE 找不到對象：{detail}", "確認 LINE_ID 正確、機器人還在群組裡。", status_code=status)
        if status == 429:
            if "monthly limit" in detail.lower():
                return MessengerError("這個月的 LINE 訊息額度用完了，訊息沒有送出", QUOTA_HINT, status_code=status)
            return MessengerError("LINE 說發送太頻繁", "稍後會自動重試。", retryable=True, status_code=status)
        if status >= 500:
            return MessengerError(f"LINE 伺服器暫時有問題（HTTP {status}）", "通常過幾分鐘就好，程式會自動重試。",
                                  retryable=True, status_code=status)
        return MessengerError(f"LINE 回應錯誤（HTTP {status}）：{detail}", status_code=status)
