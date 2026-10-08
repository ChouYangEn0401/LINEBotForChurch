"""傳到伺服器管理員 Telegram 的播報：後台開了、關了、當掉了，以及每一次發送的結果。

為什麼不直接用 ``core/login_codes.py`` 的 ``TelegramSender``：那邊傳失敗一定要丟出例外
（登入碼沒送到就不能讓人繼續登入）；這邊剛好相反——通知傳不出去**絕對不能**影響提醒本身，
所以這裡一律吞掉、只寫進 ``data/church_bot.log``。

設定共用登入碼那一組（``.env`` 的 ``TELEGRAM_BOT_TOKEN``、``SERVER_MANAGER_TELEGRAM_ID``，
在網頁「全教會設定 → 伺服器管理員」就能填）。沒填 = 整個播報靜悄悄地不做事，其他功能照常。

Telegram 不算 LINE 的則數，所以這裡可以放心多講一點；會花錢的 LINE 彙報在 ``service.py``。
"""

from __future__ import annotations

import datetime as dt
import logging
import os

import httpx

from church_bot.config import Paths, read_env_file
from church_bot.core.history import History

log = logging.getLogger(__name__)

TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
CHAT_ENV = "SERVER_MANAGER_TELEGRAM_ID"
TIMEOUT = 10.0
RUNTIME_KEY = "service_runtime"  # 共用資料庫裡的那一格：後台現在開著沒、上一次是怎麼結束的


def _env(paths: Paths, key: str) -> str:
    """.env 的值（系統環境變數優先）。每次重讀，網頁上填完 Telegram 不用重開程式。"""
    return (os.environ.get(key) or read_env_file(paths.church.env_file).get(key, "")).strip()


class Notifier:
    """伺服器管理員的 Telegram。沒設定好就安靜地什麼都不做，呼叫的人不用先檢查。"""

    def __init__(self, paths: Paths) -> None:
        self.paths = paths.church

    @property
    def enabled(self) -> bool:
        return bool(_env(self.paths, TOKEN_ENV) and _env(self.paths, CHAT_ENV))

    def send(self, text: str) -> bool:
        """傳一則給伺服器管理員。永遠不丟例外；回傳有沒有真的傳出去。"""
        token, chat_id = _env(self.paths, TOKEN_ENV), _env(self.paths, CHAT_ENV)
        if not (token and chat_id):
            return False
        try:
            # 刻意不用 parse_mode：牧區名稱、群組名稱裡出現 < & * _ 都不會把訊息弄壞
            resp = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=TIMEOUT,
                              json={"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True})
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("傳 Telegram 通知失敗（連不上）：%s", exc)
            return False
        if not data.get("ok"):
            log.warning("傳 Telegram 通知失敗：%s", data.get("description"))  # 不印 token
            return False
        return True


# --------------------------------------------------------------------------- 後台開著沒

def _now_text() -> str:
    return dt.datetime.now().astimezone().strftime("%Y/%m/%d %H:%M:%S")


def record_start(history: History) -> dict:
    """記下「後台現在開著」，並回傳**上一次**那一筆（給啟動通知判斷有沒有正常關閉用）。"""
    before = history.get_state(RUNTIME_KEY)
    history.set_state(RUNTIME_KEY, {"running": True, "clean": False, "pid": os.getpid(),
                                    "started_at": _now_text()})
    return before


def record_stop(history: History, *, clean: bool, reason: str) -> None:
    """正常收工時記一筆。被強制關掉、斷電、當掉時這行根本跑不到——那正是下次啟動要講的事。"""
    state = history.get_state(RUNTIME_KEY)
    state.update({"running": False, "clean": clean, "reason": reason, "stopped_at": _now_text()})
    history.set_state(RUNTIME_KEY, state)


def previous_shutdown_line(before: dict) -> str:
    """上一次是怎麼結束的。正常結束（或根本沒有上一次）回空字串，不用講。"""
    if not before or before.get("clean"):
        return ""
    started = before.get("started_at") or "（不知道什麼時候）"
    return f"⚠️ 上一次沒有正常關閉（{started} 開的那一次）——可能當掉、被關掉或斷電。"
