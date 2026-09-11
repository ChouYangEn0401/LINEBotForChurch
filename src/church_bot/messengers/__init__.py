"""訊息發送端。依設定 ``messenger.kind`` 建立對應的實作。"""

from __future__ import annotations

from church_bot.config import Paths, Settings
from church_bot.errors import ConfigError
from church_bot.messengers.base import Messenger, Quota, SendResult
from church_bot.messengers.console import ConsoleMessenger
from church_bot.messengers.line import LineMessenger

MESSENGER_KINDS_ZH = {
    "line": "LINE（正式發送）",
    "console": "測試模式（寫到 data/outbox.log，不會真的送出）",
}


def build_messenger(settings: Settings, paths: Paths) -> Messenger:
    kind = settings.messenger.kind
    if kind == "line":
        return LineMessenger(settings.line.channel_access_token, settings.line.timeout_seconds)
    if kind == "console":
        return ConsoleMessenger(paths.data_dir / "outbox.log")
    raise ConfigError(f"不認得的發送方式：{kind}", f"可以用：{'、'.join(MESSENGER_KINDS_ZH)}")


__all__ = ["MESSENGER_KINDS_ZH", "Messenger", "Quota", "SendResult", "build_messenger"]
