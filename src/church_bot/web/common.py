"""管理網頁共用的零件：整個網頁一份的物件（教會、排程、Webhook、畫面）、牧區的網址、兩層密碼。

網址怎麼分（見 docs/MINISTRIES.md）：
* ``/``、``/settings``、``/help``…：整個教會那一層（首頁是所有牧區）。
* ``/m/<編號>/…``：某個牧區自己的後台，裡面的每一頁跟單一牧區時一模一樣，只是網址前面多了牧區。

兩層密碼：
* 第一層是整個網站（.env 的 UI_PASSWORD），沒登入什麼都看不到，Webhook 除外。
* 第二層是牧區自己決定要不要設（church.yaml 存雜湊）。進那個牧區要輸入一次，瀏覽器記住；
  密碼一改，大家都要重新輸入。忘記了只能在那台電腦上（127.0.0.1）清掉，或用指令列。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from church_bot import __version__
from church_bot.config import WEEKDAY_ZH, Paths, read_env_file
from church_bot.core.quota import QuotaSnapshot
from church_bot.messengers import MESSENGER_KINDS_ZH
from church_bot.ministries import Church, Unit
from church_bot.models import TRIGGER_ZH, DeliveryStatus, RunReport
from church_bot.remote_config import Verifier
from church_bot.scheduler import BotScheduler, format_when
from church_bot.service import QUOTA_LOW_THRESHOLD
from church_bot.sources import SOURCE_KINDS_ZH
from church_bot.tables import describe_line_id, fmt_list
from church_bot.web.overview import issue_link
from church_bot.webhook import WebhookHandler

HERE = Path(__file__).parent
ADMIN_COOKIE = "church_bot_admin"  # 右上角「切換身分」（只是收起進階畫面，不是權限）
SESSION_COOKIE = "church_bot_session"
LAYOUTS_ZH = {"auto": "自動判斷（推薦）", "wide": "日期在左、一列一次聚會", "long": "一列一項服事",
              "matrix": "日期在上、一欄一次聚會"}


def safe_next(url: str) -> str:
    """表單帶回來的「回到哪一頁」只能是站內路徑，不能被拿來跳到外部網址。"""
    return url if url.startswith("/") and not url.startswith("//") else "/"


def redirect(url: str, msg: str = "", level: str = "ok") -> RedirectResponse:
    if msg:
        path, hash_mark, fragment = url.partition("#")  # 訊息參數要放在 #段落 前面，瀏覽器才會送到伺服器
        url = f"{path}{'&' if '?' in path else '?'}msg={quote(msg)}&level={level}{hash_mark}{fragment}"
    return RedirectResponse(url, status_code=303)


def mask(secret: str) -> str:
    return f"已設定（結尾 …{secret[-4:]}）" if len(secret) >= 8 else ("已設定" if secret else "")


def run_summary(report: RunReport) -> tuple[str, str]:
    """把一次執行結果濃縮成一句話 + 顏色，給畫面上方的提示用。"""
    sent, failed, skipped = report.count_sent, report.count_failed, report.count_skipped
    if failed or report.has_errors:
        return f"有問題：送出 {sent} 則、失敗 {failed} 則，請看下面的說明", "error"
    if sent:
        return f"已送出 {sent} 則提醒" + (f"（{skipped} 則之前已送過，略過）" if skipped else ""), "ok"
    if skipped:
        return f"這次的提醒之前已經送過了（{skipped} 則），沒有重複發送。要再送一次請按「重新發送」", "warn"
    return "這次沒有要發送的訊息，請看下面的說明", "warn"


def quota_json(snapshot: QuotaSnapshot | None) -> dict[str, Any] | None:
    """本月用量那一格要顯示的東西。None = 不適用（測試模式、關掉額度檢查），那一格就不出現。

    畫面和 /api/quota 共用同一份：app.js 收到新的就直接換掉那一格的字，不用重新整理。
    用量是整個 LINE 帳號一份，首頁和每個牧區的主控台看到的是同一個數字。
    """
    if snapshot is None:
        return None
    remaining = snapshot.remaining
    return {"value": snapshot.value_text(), "detail": snapshot.detail_text(dt.datetime.now().astimezone()),
            "low": remaining is not None and remaining < QUOTA_LOW_THRESHOLD,
            "stale": bool(snapshot.error) or not snapshot.known}


def is_local(request: Request) -> bool:
    """這個請求是不是坐在那台電腦前面打開的（不是透過免費模式的臨時網址連進來）。

    免費模式的 cloudflared 也是從 127.0.0.1 連進來，所以還要看它加上的標頭；外面的人沒辦法拿掉這些標頭。
    """
    host = request.client.host if request.client else ""
    forwarded = any(h in request.headers for h in ("x-forwarded-for", "cf-connecting-ip", "x-forwarded-host"))
    return host in ("127.0.0.1", "::1", "localhost", "testclient") and not forwarded


@dataclass(frozen=True, slots=True)
class MinistryView:
    """某個牧區的後台：網址前綴、它的 BotService、它的資料夾。"""

    unit: Unit

    @property
    def id(self) -> str:
        return self.unit.id

    @property
    def name(self) -> str:
        return self.unit.name

    @property
    def service(self):  # noqa: ANN201 - BotService
        return self.unit.service

    @property
    def paths(self) -> Paths:
        return self.unit.service.paths

    @property
    def base(self) -> str:
        return f"/m/{self.id}"

    def url(self, path: str = "/") -> str:
        return self.base + (path if path.startswith("/") else "/" + path)

    def redirect(self, path: str = "/", msg: str = "", level: str = "ok") -> RedirectResponse:
        return redirect(self.url(path), msg, level)


class LoginRequired(Exception):
    pass


class MinistryLocked(Exception):
    def __init__(self, unit: Unit, next_url: str) -> None:
        super().__init__(unit.id)
        self.unit = unit
        self.next_url = next_url


def ministry_cookie(ministry_id: str) -> str:
    return f"church_bot_m_{ministry_id}"


def ministry_token(password_hash: str) -> str:
    """第二層密碼的 cookie 值：跟著密碼雜湊變，換密碼就全部失效。"""
    return hashlib.sha256(f"ministry:{password_hash}".encode()).hexdigest()


class Web:
    """整個網頁一份：教會、排程、Webhook、畫面模板。各頁的程式都從這裡拿東西。"""

    def __init__(self, paths: Paths) -> None:
        self.paths = paths.church
        self.church = Church(self.paths)
        self.scheduler = BotScheduler(self.church)
        self.webhook = WebhookHandler(self.church, Verifier(), on_settings_changed=self.scheduler.reload)
        self.templates = Jinja2Templates(directory=str(HERE / "templates"))
        asset_hash = hashlib.sha1()
        for static in sorted((HERE / "static").glob("*")):
            asset_hash.update(static.read_bytes())
        self.templates.env.globals.update(
            version=__version__, asset_v=asset_hash.hexdigest()[:10], weekday_zh=WEEKDAY_ZH,
            describe_line_id=describe_line_id, fmt_list=fmt_list, status_zh={s.value: s.zh for s in DeliveryStatus},
            trigger_zh=TRIGGER_ZH, source_kinds=SOURCE_KINDS_ZH, messenger_kinds=MESSENGER_KINDS_ZH,
            layouts=LAYOUTS_ZH, issue_link=issue_link,
        )

    # ------------------------------------------------------------------ 第一層：整個網站的密碼

    def current_password(self) -> str:
        return os.environ.get("UI_PASSWORD") or read_env_file(self.paths.env_file).get("UI_PASSWORD", "")

    @staticmethod
    def session_token(password: str) -> str:
        return hashlib.sha256(password.encode()).hexdigest()

    def require_login(self, request: Request) -> None:
        """沒設密碼＝直接放行；設了密碼＝一定要有對得上目前密碼的 session cookie，不然一律擋下來、
        直接顯示登入畫面（不用跳轉），改密碼後舊的 cookie 立刻對不上、所有人都要重新登入。"""
        password = self.current_password()
        if not password:
            return
        if hmac.compare_digest(request.cookies.get(SESSION_COOKIE, ""), self.session_token(password)):
            return
        raise LoginRequired()

    # ------------------------------------------------------------------ 第二層：牧區自己的密碼

    def ministry(self, request: Request, mid: str) -> MinistryView:
        """網址裡的牧區（/m/<編號>/…）。找不到 → 404；牧區設了密碼而這個瀏覽器還沒輸入過 → 顯示牧區的登入畫面。"""
        ministry = self.church.config().get(mid)
        if ministry is None:
            raise HTTPException(404, f"找不到牧區「{mid}」，可能已經移除了。回首頁看現有的牧區。")
        view = MinistryView(Unit(ministry, self.church.service(ministry.id)))
        if ministry.has_password and not hmac.compare_digest(
                request.cookies.get(ministry_cookie(mid), ""), ministry_token(ministry.password_hash)):
            next_url = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            raise MinistryLocked(view.unit, next_url if request.method == "GET" else view.base + "/")
        return view

    # ------------------------------------------------------------------ 畫面

    def page(self, request: Request, name: str, m: MinistryView | None = None, **ctx: Any) -> HTMLResponse:
        ctx.setdefault("flash", request.query_params.get("msg", ""))
        ctx.setdefault("flash_level", request.query_params.get("level", "ok"))
        pending = self.webhook.verifier.current()
        today = dt.date.today()
        if m is not None:
            status = self.scheduler.status_for(m.id)
            next_run = self.scheduler.next_run_text(m.id)
            schedule_text = status if self.scheduler.next_run(m.id) else "自動發送關著（手動或 Telegram 發）"
        else:
            upcoming = self.scheduler.next_any()
            status = self.scheduler.status
            next_run = format_when(upcoming[1]) if upcoming else "（沒有排程）"
            schedule_text = status
            if upcoming and len(self.scheduler.statuses) > 1:
                who = self.church.config().get(upcoming[0])
                next_run = f"{who.name if who else upcoming[0]} {next_run}"
        base = {"nav": name.removesuffix(".html"), "schedule_status": status, "schedule_text": schedule_text,
                "next_run": next_run, "has_password": bool(self.current_password()),
                "pending_change": pending,
                "pending_minutes": self.webhook.verifier.minutes_left(pending) if pending else 0,
                "admin_mode": request.cookies.get(ADMIN_COOKIE) == "1", "current_path": request.url.path,
                "today_text": f"{today.isoformat()} · 週{'一二三四五六日'[today.weekday()]}",
                "m": m, "mb": m.base if m else "", "ministries": self.church.ministries()}
        return self.templates.TemplateResponse(request, name, {**base, **ctx})
