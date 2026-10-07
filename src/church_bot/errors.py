"""Exception hierarchy.

每個錯誤都帶一個 ``hint``：給「不懂電腦的人」看的下一步建議。
UI / CLI / 管理員通知都會把 message + hint 一起顯示，確保問題不會被靜默吞掉。
"""

from __future__ import annotations


class ChurchBotError(Exception):
    """Base class. ``message`` 說發生什麼事，``hint`` 說該怎麼辦。"""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        return f"{self.message}（建議：{self.hint}）" if self.hint else self.message


class ConfigError(ChurchBotError):
    """設定檔讀不到 / 格式錯。"""


class SourceNotSetError(ConfigError):
    """還沒接服事表（剛建好的牧區）。跟其他設定錯誤分開，主控台才會帶人去「服事表」頁接上，而不是設定頁。"""


class TableError(ChurchBotError):
    """對照表（CSV）讀不到 / 格式錯。"""


class FileLockedError(TableError):
    """存檔時檔案被別的程式打開（Windows 上通常是 Excel）鎖住了。"""


class SourceError(ChurchBotError):
    """讀服事表失敗（Google Sheet 權限、網路、格式…）。"""


class MessengerError(ChurchBotError):
    """送訊息失敗。``retryable`` 表示稍後重試有機會成功（網路、限流）。"""

    def __init__(
        self,
        message: str,
        hint: str = "",
        *,
        retryable: bool = False,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message, hint)
        self.retryable = retryable
        self.status_code = status_code
