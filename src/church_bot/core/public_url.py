"""「外面連得到的網址」：免費模式每次重開，cloudflared 給的臨時網址都不一樣。

為什麼要記下來：管理員換了網址就得一個一個貼給其他管理員。記起來之後，
誰想進管理網頁，在 LINE 打「/服務網址」機器人就會回最新的那一個（見 webhook.py 的 service_url 指令）。

怎麼知道網址是什麼：
* LINE 打我們的 Webhook 時，請求裡的 Host 就是「外面看到的網址」——而且 LINE 真的連得到，
  所以這個來源最可靠（只在簽章驗過之後才記，不然誰都能偽造 Host 餵假網址進來）。
* 免費模式把網址登記給 LINE 的時候也記一次（見 tunnel.register_webhook），
  這樣還沒有人打過指令就已經知道。

跟用量快照一樣放在資料庫的 state 表（見 history.py）：管理網頁和免費模式是兩個不同的程式。
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from church_bot.core.history import History

log = logging.getLogger(__name__)

STATE_KEY = "service_url"
# 這些是「只有那台電腦自己連得到」的位址，記下來對別人沒有意義
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"})
SOURCE_ZH = {"webhook": "LINE 剛剛連到這個網址", "register": "登記給 LINE 的網址"}


def public_base(host: str, proto: str = "https") -> str:
    """從 HTTP 請求的 Host（或 X-Forwarded-Host）推出對外網址；本機位址一律回傳 ""。"""
    host = (host or "").split(",")[0].strip()
    if not host or host.rsplit(":", 1)[0].strip("[]") in LOCAL_HOSTS or host in LOCAL_HOSTS:
        return ""
    proto = (proto or "https").split(",")[0].strip() or "https"
    return f"{proto}://{host}"


@dataclass(frozen=True, slots=True)
class ServiceUrl:
    url: str = ""
    seen_at: dt.datetime | None = None
    source: str = ""  # webhook / register（見 SOURCE_ZH）

    @property
    def known(self) -> bool:
        return bool(self.url)

    def describe(self) -> str:
        """「這個網址是什麼時候、怎麼知道的」。"""
        when = self.seen_at.strftime("%m/%d %H:%M") if self.seen_at else ""
        source = SOURCE_ZH.get(self.source, "")
        return "・".join(part for part in (when, source) if part)

    def to_state(self) -> dict:
        return {"url": self.url, "seen_at": self.seen_at.isoformat(timespec="seconds") if self.seen_at else "",
                "source": self.source}

    @classmethod
    def from_state(cls, data: dict) -> ServiceUrl:
        try:
            seen_at = dt.datetime.fromisoformat(str(data.get("seen_at")))
        except (TypeError, ValueError):
            seen_at = None
        url = str(data.get("url") or "")
        return cls(url=url if url.startswith(("http://", "https://")) else "", seen_at=seen_at,
                   source=str(data.get("source") or ""))


def load_service_url(history: History) -> ServiceUrl:
    return ServiceUrl.from_state(history.get_state(STATE_KEY))


def save_service_url(history: History, url: str, source: str = "webhook") -> ServiceUrl:
    """記下對外網址（同一個網址就只是把「最後看到的時間」更新）。空網址當作沒這回事。"""
    fresh = ServiceUrl(url=url.rstrip("/"), seen_at=dt.datetime.now().astimezone(), source=source)
    if not fresh.known:
        return load_service_url(history)
    if fresh.url != load_service_url(history).url:
        log.info("對外網址變成 %s（%s）", fresh.url, SOURCE_ZH.get(source, source))
    history.set_state(STATE_KEY, fresh.to_state())
    return fresh
