"""管理網頁共用的零件：整個網頁一份的物件（教會、排程、Webhook、畫面）、牧區的網址、三種身分（訪客、牧區管理員、伺服器管理員）。

網址怎麼分（見 docs/MINISTRIES.md）：
* ``/``、``/settings``、``/help``…：整個教會那一層（首頁是所有牧區）。
* ``/m/<編號>/…``：某個牧區自己的後台，裡面的每一頁跟單一牧區時一模一樣，只是網址前面多了牧區。

三種身分（伺服器檢查，不是只把按鈕藏起來）：
* 訪客：輸入網站密碼（.env 的 UI_PASSWORD）進來的人。看得到首頁、可以新增牧區（新增時要設牧區密碼）；
  要進任何一個牧區都要那個牧區的密碼。沒登入什麼都看不到，Webhook 除外。
* 牧區管理員：輸入了某個牧區的密碼（church.yaml 存雜湊），只能管那一個牧區；
  瀏覽器記 30 天，密碼一改大家都要重新輸入。還沒設密碼的牧區只有伺服器管理員進得去。
* 伺服器管理員（server_manager）：另一組管理者密碼（.env 的 SERVER_MANAGER_PASSWORD）登入，
  所有牧區都進得去（牧區密碼對他無效），管全教會設定、任何牧區的密碼、重新啟動。
  還沒設管理者密碼時，坐在那台電腦前面（本機）的人就是伺服器管理員（第一次設定用）。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from church_bot import __version__
from church_bot.config import WEEKDAY_ZH, Paths, read_env_file
from church_bot.core import message_template
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
ADMIN_COOKIE = "church_bot_admin"  # 右上角「進階頁面」（只是收起進階畫面，不是權限）
SESSION_COOKIE = "church_bot_session"
MANAGER_COOKIE = "church_bot_manager"
MANAGER_SESSION = dt.timedelta(hours=12)
MIN_MANAGER_PASSWORD = 10
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


class ManagerRequired(Exception):
    """這件事只有伺服器管理員能做。"""

    def __init__(self, next_url: str = "/") -> None:
        super().__init__(next_url)
        self.next_url = next_url


def ministry_cookie(ministry_id: str) -> str:
    return f"church_bot_m_{ministry_id}"


def ministry_token(password_hash: str) -> str:
    """第二層密碼的 cookie 值：跟著密碼雜湊變，換密碼就全部失效。"""
    return hashlib.sha256(f"ministry:{password_hash}".encode()).hexdigest()


HISTORY_LIMIT = 150


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
            layouts=LAYOUTS_ZH, issue_link=issue_link, message_tags=message_template.TAGS,
        )

    # ------------------------------------------------------------------ 第一層：整個網站的密碼

    def env(self, key: str) -> str:
        """.env 的值（系統環境變數優先）；每次重新讀檔，網頁上改完不用重開。"""
        return (os.environ.get(key) or read_env_file(self.paths.env_file).get(key, "")).strip()

    def current_password(self) -> str:
        return self.env("UI_PASSWORD")

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
        if self.manager_password() and self.is_manager(request):
            # 用管理者密碼登入過的人一定進得了網站（例如網站密碼剛換）。還沒設管理者密碼時「本機 = 管理者」
            # 只是第一次設定用，不能拿來跳過網站密碼
            return
        raise LoginRequired()

    # ------------------------------------------------------------------ 伺服器管理員（server_manager）

    def manager_password(self) -> str:
        return self.env("SERVER_MANAGER_PASSWORD")

    def _secret(self) -> bytes:
        """簽 cookie 用的金鑰：第一次用時隨機產生、存在教會共用的資料庫，重開程式（例如按「重新啟動」）也不會登出。"""
        state = self.church.shared.get_state("web_secret")
        if not state.get("key"):
            state = {"key": secrets.token_hex(32)}
            self.church.shared.set_state("web_secret", state)
        return bytes.fromhex(state["key"])

    def _manager_sig(self, expires: str) -> str:
        bound = hashlib.sha256(self.manager_password().encode()).hexdigest()  # 換管理者密碼 → 舊 cookie 全部失效
        return hmac.new(self._secret(), f"manager|{expires}|{bound}".encode(), hashlib.sha256).hexdigest()

    def manager_cookie_value(self) -> str:
        expires = str(int((dt.datetime.now() + MANAGER_SESSION).timestamp()))
        return f"{expires}.{self._manager_sig(expires)}"

    def is_manager(self, request: Request) -> bool:
        if not self.manager_password():
            return is_local(request)  # 還沒設管理者密碼：坐在這台電腦前面的人就是管理者（第一次設定用）
        expires, _, sig = request.cookies.get(MANAGER_COOKIE, "").partition(".")
        if not expires.isdigit() or int(expires) < dt.datetime.now().timestamp():
            return False
        return hmac.compare_digest(sig, self._manager_sig(expires))

    def require_manager(self, request: Request) -> None:
        if not self.is_manager(request):
            raise ManagerRequired(request.url.path if request.method == "GET" else "/")

    # ------------------------------------------------------------------ 牧區管理員：牧區自己的密碼

    def unlocked(self, request: Request, ministry_id: str) -> bool:
        """這個瀏覽器能不能管這個牧區：伺服器管理員一律可以；其他人要輸入過那個牧區的密碼。"""
        if self.is_manager(request):
            return True
        ministry = self.church.config().get(ministry_id)
        return bool(ministry and ministry.has_password and hmac.compare_digest(
            request.cookies.get(ministry_cookie(ministry_id), ""), ministry_token(ministry.password_hash)))

    def ministry(self, request: Request, mid: str) -> MinistryView:
        """網址裡的牧區（/m/<編號>/…）。找不到 → 404；不是伺服器管理員、也還沒輸入那個牧區的密碼 → 牧區的登入畫面。"""
        ministry = self.church.config().get(mid)
        if ministry is None:
            raise HTTPException(404, f"找不到牧區「{mid}」，可能已經移除了。回首頁看現有的牧區。")
        view = MinistryView(Unit(ministry, self.church.service(ministry.id)))
        if not self.unlocked(request, mid):
            next_url = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            raise MinistryLocked(view.unit, next_url if request.method == "GET" else view.base + "/")
        return view

    def role(self, request: Request, m: MinistryView | None = None) -> str:
        if self.is_manager(request):
            return "manager"
        return "admin" if m is not None else "visitor"

    # ------------------------------------------------------------------ 變更紀錄（見 core/versions.py）

    def history_page(self, request: Request, m: MinistryView | None) -> HTMLResponse:
        store = self.church.versions
        changes = store.changes(m.id if m else "", HISTORY_LIMIT)
        return self.page(request, "history.html", m, changes=changes, limit=HISTORY_LIMIT,
                         diffs={c.id: store.diff(c) for c in changes})

    def restore(self, change_id: int, scope: str) -> tuple[str, str]:
        """把某一筆修改的檔案還原成「這次修改之前」；回傳 (訊息, 顏色)。還原本身也會留一筆紀錄。"""
        from church_bot.core import versions
        from church_bot.files import write_bytes

        store = self.church.versions
        change = store.change(change_id)
        if change is None or change.scope != scope:
            return "找不到這筆紀錄", "error"
        content = store.content(change.before)
        if content is None:
            return "這一筆沒有「修改之前」的內容可以還原", "warn"
        path = (self.paths.root / change.file).resolve()
        if store.where(path) != (scope, change.file):
            return "這個檔案不在可以還原的範圍", "error"
        with versions.source(f"還原（第 {change.id} 筆之前）"):
            write_bytes(path, content)
        self.scheduler.reload()  # 還原的可能是發送時間、牧區清單
        return f"已把「{change.label}」還原成 {change.when} 修改之前的樣子", "ok"

    # ------------------------------------------------------------------ 畫面

    def page(self, request: Request, name: str, m: MinistryView | None = None, **ctx: Any) -> HTMLResponse:
        ctx.setdefault("flash", request.query_params.get("msg", ""))
        ctx.setdefault("flash_level", request.query_params.get("level", "ok"))
        pending = self.webhook.verifier.current()
        if pending is not None and not (self.is_manager(request) or self.unlocked(request, pending.ministry_id)):
            pending = None  # LINE「/設定」等驗證的那一條：只給能管那個牧區的人看、核准
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
                "m": m, "mb": m.base if m else "", "ministries": self.church.ministries(),
                "role": self.role(request, m), "is_manager": self.is_manager(request),
                "manager_set": bool(self.manager_password())}
        return self.templates.TemplateResponse(request, name, {**base, **ctx})
