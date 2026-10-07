"""伺服器管理員（server_manager）登入：/manager/login。

* 在那台電腦上（本機）：管理者密碼就好——坐在電腦前面就是最後一道退路，手機不在身邊也進得去。
* 從外面（免費模式的臨時網址）：管理者密碼 + 下面任一個（照 CloudServicesPlatform 的做法）：
  - 驗證器 App（Google Authenticator 這類）的 6 位數（core/totp.py）
  - Telegram 收到的 6 位數（core/login_codes.py，另一半留在這一頁）
  兩個都沒設定時，從外面一律不能用管理者身分登入。
* 過了密碼那一關，瀏覽器只拿到一個「登入中」的小 cookie（5 分鐘、只在記憶體）；第二關錯 3 次就要從頭來。
* 從外面連續錯 5 次（15 分鐘內）就暫停外面的管理者登入 15 分鐘；本機不受影響（陌生人不能把你鎖在自己電腦外面）。

設定都在 .env：SERVER_MANAGER_PASSWORD、SERVER_MANAGER_TOTP_SECRET、TELEGRAM_BOT_TOKEN、SERVER_MANAGER_TELEGRAM_ID
（在「全教會設定 → 伺服器管理員」設定，見 app.py）。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import secrets
import threading
from dataclasses import dataclass

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from church_bot.core import totp
from church_bot.core.login_codes import LoginCodeError, LoginCodes, TelegramSender
from church_bot.web.common import MANAGER_COOKIE, MANAGER_SESSION, SESSION_COOKIE, Web, is_local, redirect, safe_next

log = logging.getLogger(__name__)

HALF_COOKIE = "church_bot_manager_half"
HALF_TTL = dt.timedelta(minutes=5)
HALF_ATTEMPTS = 3
FAIL_WINDOW = dt.timedelta(minutes=15)
FAIL_LIMIT = 5
LOCKOUT = dt.timedelta(minutes=15)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> dt.datetime:
    return dt.datetime.now().astimezone()


@dataclass(slots=True)
class _Half:
    expires_at: dt.datetime
    attempts_left: int


class ManagerLogins:
    """過了密碼、還差第二關的登入（只在記憶體），以及從外面登入失敗的次數。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._halves: dict[str, _Half] = {}
        self._failures: list[dt.datetime] = []
        self._locked_until: dt.datetime | None = None
        self.codes = LoginCodes()
        self.totp = totp.Verifier()

    def start(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            now = _now()
            self._halves = {k: v for k, v in self._halves.items() if v.expires_at > now}
            self._halves[_hash(token)] = _Half(now + HALF_TTL, HALF_ATTEMPTS)
        return token

    def valid(self, token: str) -> bool:
        with self._lock:
            half = self._halves.get(_hash(token or ""))
            return half is not None and half.expires_at > _now()

    def wrong(self, token: str) -> bool:
        """第二關錯一次；回傳 True = 還可以再試，False = 這次登入作廢（要從密碼重來）。"""
        self.fail()
        with self._lock:
            half = self._halves.get(_hash(token or ""))
            if half is None:
                return False
            half.attempts_left -= 1
            if half.attempts_left <= 0:
                self._halves.pop(_hash(token), None)
                return False
            return True

    def finish(self, token: str) -> None:
        with self._lock:
            self._halves.pop(_hash(token or ""), None)

    def fail(self) -> None:
        with self._lock:
            now = _now()
            self._failures = [t for t in self._failures if now - t < FAIL_WINDOW] + [now]
            if len(self._failures) >= FAIL_LIMIT:
                self._locked_until = now + LOCKOUT
                self._failures = []
                log.warning("伺服器管理員從外面登入失敗太多次，暫停外面的管理者登入 %d 分鐘", LOCKOUT.seconds // 60)

    def locked(self) -> bool:
        with self._lock:
            return self._locked_until is not None and _now() < self._locked_until


def manager_routes(web: Web) -> APIRouter:
    router = APIRouter(prefix="/manager")
    logins = ManagerLogins()
    web.manager_logins = logins

    def factors() -> dict[str, bool]:
        return {"totp": bool(web.env("SERVER_MANAGER_TOTP_SECRET")),
                "telegram": bool(web.env("TELEGRAM_BOT_TOKEN") and web.env("SERVER_MANAGER_TELEGRAM_ID"))}

    def show(request: Request, step: str, next_url: str, error: str = "", nonce: str = "", status: int = 200):
        response = web.templates.TemplateResponse(request, "manager_login.html", {
            "step": step, "next": safe_next(next_url), "error": error, "factors": factors(), "nonce": nonce,
            "local": is_local(request), "configured": bool(web.manager_password()),
        }, status_code=status)
        return response

    def grant(request: Request, next_url: str) -> object:
        resp = redirect(safe_next(next_url), "已用伺服器管理員身分登入：所有牧區都進得去")
        resp.set_cookie(MANAGER_COOKIE, web.manager_cookie_value(), httponly=True, samesite="lax",
                        max_age=int(MANAGER_SESSION.total_seconds()))
        if site := web.current_password():  # 管理者一定也進得了網站
            resp.set_cookie(SESSION_COOKIE, web.session_token(site), httponly=True, samesite="lax",
                            max_age=60 * 60 * 24 * 30)
        token = request.cookies.get(HALF_COOKIE, "")
        logins.finish(token)
        resp.delete_cookie(HALF_COOKIE, path="/manager")
        log.warning("伺服器管理員登入（%s）", "本機" if is_local(request) else "從外面")
        return resp

    def restart(request: Request, next_url: str, message: str):
        resp = show(request, "password", next_url, message, status=401)
        resp.delete_cookie(HALF_COOKIE, path="/manager")
        return resp

    @router.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str = "/"):
        if web.is_manager(request) and web.manager_password():
            return redirect(safe_next(next))
        return show(request, "password", next)

    @router.post("/login")
    def login(request: Request, password: str = Form(""), next: str = Form("/")):
        saved = web.manager_password()
        local = is_local(request)
        if not saved:
            if local:
                return redirect(safe_next(next), "還沒設管理者密碼，在這台電腦上你本來就是伺服器管理員；"
                                                 "請到「全教會設定 → 伺服器管理員」設一組", "warn")
            return show(request, "password", next, "伺服器管理員還沒設定密碼，只能在執行機器人的那台電腦上設定。", status=401)
        if not local and logins.locked():
            return show(request, "password", next, "失敗太多次，從外面的管理者登入暫停 15 分鐘。", status=429)
        if not secrets.compare_digest(password.strip(), saved):
            if not local:
                logins.fail()
            return show(request, "password", next, "管理者密碼不對", status=401)
        if local:
            return grant(request, next)
        if not any(factors().values()):
            return show(request, "password", next, "從外面用管理者身分登入，要先設定驗證器 App 或 Telegram 登入碼"
                                                   "（在那台電腦上的「全教會設定 → 伺服器管理員」）。", status=403)
        resp = show(request, "second", next)
        resp.set_cookie(HALF_COOKIE, logins.start(), httponly=True, samesite="lax", path="/manager",
                        max_age=int(HALF_TTL.total_seconds()))
        return resp

    @router.post("/totp")
    def verify_totp(request: Request, code: str = Form(""), next: str = Form("/")):
        token = request.cookies.get(HALF_COOKIE, "")
        if not logins.valid(token):
            return restart(request, next, "登入逾時了，請重新輸入管理者密碼。")
        if logins.totp.check(web.env("SERVER_MANAGER_TOTP_SECRET"), code):
            return grant(request, next)
        if not logins.wrong(token):
            return restart(request, next, "驗證碼錯太多次，請重新輸入管理者密碼。")
        return show(request, "second", next, "驗證器的 6 位數不對（每 30 秒會換一組）", status=401)

    @router.post("/telegram/send")
    def telegram_send(request: Request, next: str = Form("/")):
        token = request.cookies.get(HALF_COOKIE, "")
        if not logins.valid(token):
            return restart(request, next, "登入逾時了，請重新輸入管理者密碼。")
        if not factors()["telegram"]:
            return show(request, "second", next, "Telegram 登入碼還沒設定好", status=400)
        try:
            nonce = logins.codes.start(TelegramSender(web.env("TELEGRAM_BOT_TOKEN")), web.env("SERVER_MANAGER_TELEGRAM_ID"))
        except LoginCodeError as exc:
            return show(request, "second", next, f"{exc.message}。{exc.hint}", status=503)
        return show(request, "telegram", next, nonce=nonce)

    @router.post("/telegram/verify")
    def telegram_verify(request: Request, nonce: str = Form(""), code: str = Form(""), next: str = Form("/")):
        token = request.cookies.get(HALF_COOKIE, "")
        if not logins.valid(token):
            return restart(request, next, "登入逾時了，請重新輸入管理者密碼。")
        if logins.codes.verify(nonce, code):
            return grant(request, next)
        if not logins.wrong(token):
            return restart(request, next, "登入碼錯太多次，請重新輸入管理者密碼。")
        return show(request, "telegram", next, "登入碼不對或已經過期（90 秒內有效）", nonce=nonce, status=401)

    @router.post("/logout")
    def logout():
        resp = redirect("/", "已登出伺服器管理員")
        resp.delete_cookie(MANAGER_COOKIE)
        return resp

    return router
