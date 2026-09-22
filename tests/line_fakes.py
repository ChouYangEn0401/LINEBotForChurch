"""LINE Webhook 測試用的假資料與假 LineMessenger。

放在一般模組而不是 conftest.py：pytest 載入的 conftest 和測試檔 import 的 tests.conftest 是兩份不同的模組，
有狀態的 FakeLine 放在 conftest 會變成兩個不同的類別，fixture 記的回覆測試檔讀不到。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

from church_bot.messengers.base import SendResult
from church_bot.models import OutgoingMessage

SECRET = "s3cret"


def gid(ch: str = "a") -> str:
    return "C" + ch * 32


def uid(ch: str = "b") -> str:
    return "U" + ch * 32


def sign(body: bytes, secret: str = SECRET) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


def event_body(*events: dict) -> bytes:
    return json.dumps({"events": list(events)}).encode()


def say(text: str, token: str = "r", user: str = "", chat: str = "") -> bytes:
    """某人在群組裡傳了一句話。"""
    source = {"type": "group", "groupId": chat or gid(), "userId": user or uid()}
    return event_body({"type": "message", "replyToken": token, "source": source,
                       "message": {"type": "text", "text": text}})


class FakeLine:
    """假的 LineMessenger：reply 和 send 都記在 replies（reply 記 reply token，send 記收件人）。"""

    replies: list[tuple[str, str]] = []
    profile_name = "小美"
    profile_lookups = 0

    def __init__(self, *args, **kwargs) -> None:
        pass

    @classmethod
    def reset(cls) -> None:
        cls.replies, cls.profile_name, cls.profile_lookups = [], "小美", 0

    def reply(self, token: str, text: str) -> None:
        FakeLine.replies.append((token, text))

    def reply_texts(self, token: str, texts: list) -> None:
        for text in texts[:5]:
            FakeLine.replies.append((token, text if isinstance(text, str) else text.text))

    def group_name(self, group_id: str) -> str:
        return "敬拜團"

    def member_profile(self, chat_id: str, user_id: str) -> str:
        FakeLine.profile_lookups += 1
        return FakeLine.profile_name

    def send(self, to: str, message: OutgoingMessage) -> SendResult:
        FakeLine.replies.append((to, message.text))
        return SendResult()

    def close(self) -> None:
        pass
