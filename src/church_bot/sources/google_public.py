"""讀「知道連結的人都能檢視」的 Google Sheet —— 不需要任何金鑰。

這是最簡單的做法：Google Sheet 右上角「共用」→ 一般存取權改成「知道連結的使用者」→ 角色「檢視者」，
然後把網址貼到設定頁即可。

支援的網址：
* 一般編輯網址：https://docs.google.com/spreadsheets/d/<ID>/edit#gid=<分頁代號>
  （打開你要的分頁再複製網址，網址裡就會帶 gid，程式就知道要讀哪個分頁）
* 「發佈到網路」的網址：https://docs.google.com/spreadsheets/d/e/<發佈ID>/pubhtml
"""

from __future__ import annotations

import csv
import io
import re
from urllib.parse import parse_qs, quote, urlparse

import httpx

from church_bot.errors import ConfigError, SourceError
from church_bot.models import RawSheet
from church_bot.sources.base import split_worksheets

_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]{20,})")
_PUB_RE = re.compile(r"/spreadsheets/d/e/([a-zA-Z0-9-_]{20,})")
_GID_RE = re.compile(r"[#&?]gid=(\d+)")

NOT_SHARED_HINT = (
    "打開 Google Sheet → 右上角「共用」→「一般存取權」改成「知道連結的使用者」（檢視者）。"
    "如果不想公開，請改用「Google 服務帳號」模式。"
)


def parse_sheet_url(url: str) -> tuple[str, str, bool]:
    """回傳 (試算表 ID, gid, 是否為「發佈到網路」網址)。"""
    url = (url or "").strip()
    if not url:
        raise ConfigError("還沒填 Google Sheet 網址", "到網頁「設定」貼上服事表的網址。")
    gid_match = _GID_RE.search(url)
    gid = gid_match.group(1) if gid_match else parse_qs(urlparse(url).query).get("gid", [""])[0]
    if pub := _PUB_RE.search(url):
        return pub.group(1), gid, True
    if m := _ID_RE.search(url):
        return m.group(1), gid, False
    raise ConfigError(
        "Google Sheet 網址看不懂",
        "請直接從瀏覽器網址列複製，應該長得像 https://docs.google.com/spreadsheets/d/一長串英數字/edit#gid=0",
    )


class GooglePublicSheetSource:
    def __init__(self, url: str, worksheet: str = "", timeout: float = 20.0) -> None:
        self.url = url
        self.sheet_id, self.gid, self.published = parse_sheet_url(url)
        self.worksheets = split_worksheets(worksheet)
        self.timeout = timeout

    def describe(self) -> str:
        which = "、".join(self.worksheets) if self.worksheets else (f"gid={self.gid}" if self.gid else "第一個分頁")
        return f"Google Sheet（公開連結）：{which}"

    def _export_urls(self) -> list[tuple[str, str]]:
        """回傳 [(分頁說明, CSV 下載網址)]。"""
        base = f"https://docs.google.com/spreadsheets/d/{'e/' if self.published else ''}{self.sheet_id}"
        if self.published:
            if self.worksheets:
                raise ConfigError(
                    "「發佈到網路」的網址不能用分頁名稱指定分頁",
                    "分頁名稱請留空，並在發佈時選擇要發佈的那個分頁；或改貼一般的編輯網址。",
                )
            suffix = f"&gid={self.gid}&single=true" if self.gid else ""
            return [("發佈的分頁", f"{base}/pub?output=csv{suffix}")]
        if self.worksheets:
            # gviz 用分頁名稱讀；缺點是 Google 會自動猜欄位型別，所以優先建議用 gid
            return [(name, f"{base}/gviz/tq?tqx=out:csv&sheet={quote(name)}") for name in self.worksheets]
        suffix = f"&gid={self.gid}" if self.gid else ""
        return [(f"gid={self.gid}" if self.gid else "第一個分頁", f"{base}/export?format=csv{suffix}")]

    def fetch(self) -> list[RawSheet]:
        sheets: list[RawSheet] = []
        with httpx.Client(timeout=self.timeout, follow_redirects=True,
                          headers={"User-Agent": "church-bot/0.1"}) as client:
            for label, url in self._export_urls():
                sheets.append(RawSheet(rows=self._download(client, label, url),
                                       source=f"Google Sheet（公開連結）：{label}"))
        return sheets

    def _download(self, client: httpx.Client, label: str, url: str) -> list[list[str]]:
        try:
            resp = client.get(url)
        except httpx.TimeoutException as exc:
            raise SourceError("連 Google Sheet 逾時", "檢查網路是否正常，稍後再試一次。") from exc
        except httpx.HTTPError as exc:
            raise SourceError(f"連不上 Google Sheet：{exc.__class__.__name__}", "檢查這台電腦的網路。") from exc

        landed_on_login = "accounts.google.com" in str(resp.url) or "ServiceLogin" in str(resp.url)
        content_type = resp.headers.get("content-type", "")
        if landed_on_login or resp.status_code in (401, 403):
            raise SourceError("這份 Google Sheet 沒有開放「知道連結的人可以檢視」", NOT_SHARED_HINT)
        if resp.status_code == 404:
            raise SourceError("找不到這份 Google Sheet", "網址可能複製不完整，請重新從瀏覽器網址列複製。")
        if resp.status_code == 400 and self.worksheets:
            raise SourceError(f"找不到名為「{label}」的分頁", "分頁名稱要跟 Google Sheet 下方的分頁標籤一模一樣（含空白）。")
        if resp.status_code >= 400:
            raise SourceError(f"Google Sheet 回應錯誤（HTTP {resp.status_code}）", "稍後再試；持續發生請檢查網址。")
        if "text/html" in content_type:
            raise SourceError("讀到的是網頁而不是表格資料，通常是沒有開放共用", NOT_SHARED_HINT)

        text = resp.content.decode("utf-8-sig", errors="replace")
        return list(csv.reader(io.StringIO(text)))
