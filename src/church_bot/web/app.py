"""管理網頁（FastAPI + Jinja2，伺服器端產生畫面，不需要任何前端框架或建置步驟）。

整個網站分兩層（見 docs/MINISTRIES.md）：
* 教會這一層（這個檔案）：首頁是所有牧區（新增牧區、還沒分配的群組），全教會設定（LINE 金鑰、網站密碼）、說明、登入。
* 牧區這一層（web/ministry.py）：/m/<編號>/…，裡面就是原本那一整套後台：
  主控台、服事表、同工名單（LINE 帳號、小團）、LINE 群組、發送紀錄、設定、系統檢查。

版面跟 Workflow Helper / pyDMS 同一套：側欄每一項底下是它「現在的狀況」（/m/<編號>/api/nav），
每一頁開頭是「小標 → 標題 → 一句話」，內容用帶標題列的面板，新增／編輯用右邊的抽屜。
「切換身分」只是把進階的東西收起來（cookie），不是權限；要限制誰能開網頁請設密碼（兩層，見 web/common.py）。
JSON API：/api/*、/m/<編號>/api/*（自動產生的文件在 /docs）。LINE Webhook：/line/webhook（選用）。
"""

from __future__ import annotations

import logging
import os
import secrets
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote

import jinja2
from fastapi import APIRouter, Depends, FastAPI, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from church_bot import __version__
from church_bot.church import MIN_MINISTRY_PASSWORD, add_ministry, check_password, edit_ministry, hash_password
from church_bot.config import Paths, load_settings, read_env_file, update_env_file
from church_bot.core import versions
from church_bot.core.public_url import public_base
from church_bot.errors import ChurchBotError, ConfigError
from church_bot.web.common import (
    ADMIN_COOKIE, HERE, MANAGER_COOKIE, SESSION_COOKIE, LoginRequired, ManagerRequired, MinistryLocked, Web,
    is_local, mask, ministry_cookie, ministry_token, quota_json, redirect, safe_next,
)
from church_bot.web.ministry import ministry_routes
from church_bot.webhook import SignatureError

log = logging.getLogger(__name__)


def create_app(paths: Paths) -> FastAPI:
    web = Web(paths)
    church, scheduler, webhook = web.church, web.scheduler, web.webhook

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        scheduler.start()
        try:
            yield
        finally:
            scheduler.shutdown()

    app = FastAPI(title="教會服事提醒機器人", version=__version__, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    app.state.web = web
    app.state.webhook = webhook

    ui = APIRouter(dependencies=[Depends(web.require_login)])
    # 只有伺服器管理員：全教會設定、還沒分配的群組、教會這一層的變更紀錄、清掉任何牧區的密碼
    manager = APIRouter(dependencies=[Depends(web.require_login), Depends(web.require_manager)])
    api = APIRouter(prefix="/api", tags=["API"], dependencies=[Depends(web.require_login)])

    @app.middleware("http")
    async def change_source(request: Request, call_next):
        """變更紀錄的「來源」：從網頁存的寫「管理網頁」，LINE Webhook 進來的寫「LINE」（見 core/versions.py）。"""
        token = versions.set_source("LINE" if request.url.path == "/line/webhook" else "管理網頁")
        try:
            return await call_next(request)
        finally:
            versions.reset_source(token)

    # ------------------------------------------------------------------ 登入（第一層）與錯誤畫面

    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, exc: LoginRequired) -> HTMLResponse:
        next_url = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return web.templates.TemplateResponse(request, "login.html", {"next": next_url}, status_code=401)

    @app.post("/login")
    async def login(request: Request, password: str = Form(""), next: str = Form("/")):
        next = safe_next(next)  # next 是網址列裡的路徑，不能是外部網址，避免被拿來做開放重導向釣魚
        saved = web.current_password()
        if saved and secrets.compare_digest(password, saved):
            resp = RedirectResponse(next or "/", status_code=303)
            resp.set_cookie(SESSION_COOKIE, web.session_token(saved), httponly=True, samesite="lax",
                            max_age=60 * 60 * 24 * 30)
            return resp
        return web.templates.TemplateResponse(request, "login.html", {"next": next, "error": "密碼不對，再試一次"},
                                              status_code=401)

    @app.post("/logout")
    def logout():
        resp = redirect("/")
        resp.delete_cookie(SESSION_COOKIE)
        resp.delete_cookie(MANAGER_COOKIE)
        for ministry in church.ministries():
            resp.delete_cookie(ministry_cookie(ministry.id))
        return resp

    @ui.post("/admin-mode")
    def admin_mode(enabled: str = Form(""), next: str = Form("/")):
        """右上角「進階頁面」：打開 = 側欄多出設定、系統檢查、變更紀錄，畫面上多出 ID、檔案位置這類細節。
        只是把畫面收起來，不是權限：權限看身分（訪客、牧區管理員、伺服器管理員，見 web/common.py）。"""
        on = enabled == "1"
        resp = redirect(safe_next(next), "已打開進階頁面：側欄多了設定、系統檢查、變更紀錄" if on else "已收起進階頁面")
        if on:
            resp.set_cookie(ADMIN_COOKIE, "1", samesite="lax", max_age=60 * 60 * 24 * 365)
        else:
            resp.delete_cookie(ADMIN_COOKIE)
        return resp

    # --- 第二層：牧區自己的密碼 ---

    @app.exception_handler(MinistryLocked)
    async def ministry_locked(request: Request, exc: MinistryLocked):
        if "/api/" in request.url.path:  # 首頁、側欄用 JavaScript 來問的：回「要密碼」，不要回一整頁登入畫面
            return JSONResponse({"detail": f"「{exc.unit.name}」要先輸入牧區密碼"}, status_code=401)
        response = web.page(request, "ministry_login.html", None, locked=exc.unit.ministry, next=exc.next_url)
        response.status_code = 401
        return response

    @app.exception_handler(ManagerRequired)
    async def manager_required(request: Request, exc: ManagerRequired):
        if "/api/" in request.url.path:
            return JSONResponse({"detail": "只有伺服器管理員可以"}, status_code=403)
        response = web.page(request, "error.html", None, message="這裡只有伺服器管理員可以進來",
                            hint="請用伺服器管理員的密碼登入（右上角「伺服器管理員」）。")
        response.status_code = 403
        return response

    @ui.post("/m/{mid}/unlock")
    def ministry_unlock(request: Request, mid: str, password: str = Form(""), next: str = Form("")):
        ministry = church.config().get(mid)
        if ministry is None:
            return redirect("/", "找不到這個牧區", "error")
        target = next if next.startswith(f"/m/{mid}/") else f"/m/{mid}/"
        if not ministry.has_password:
            return redirect(target)
        if not check_password(password, ministry.password_hash):
            return web.page(request, "ministry_login.html", None, locked=ministry, next=target,
                            error="密碼不對，再試一次")
        resp = redirect(target)
        resp.set_cookie(ministry_cookie(mid), ministry_token(ministry.password_hash), httponly=True, samesite="lax",
                        max_age=60 * 60 * 24 * 30)
        return resp

    @manager.post("/m/{mid}/forgot-password")
    def ministry_forgot_password(mid: str):
        """忘記牧區密碼：只有伺服器管理員能清掉（或在那台電腦上執行 cli.bat 牧區 clear-password）。"""
        ministry = church.config().get(mid)
        if ministry is None:
            return redirect("/", "找不到這個牧區", "error")
        edit_ministry(web.paths, mid, password_hash="")
        log.warning("伺服器管理員清掉了「%s」的牧區密碼", ministry.name)
        return redirect(f"/m/{mid}/settings#ministry", f"已清掉「{ministry.name}」的密碼，記得重新設一個")

    @app.exception_handler(ChurchBotError)
    async def friendly_error(request: Request, exc: ChurchBotError) -> HTMLResponse:
        return web.page(request, "error.html", None, message=exc.message, hint=exc.hint)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        if "/api/" in request.url.path or request.url.path.startswith(("/docs", "/openapi")):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        response = web.page(request, "error.html", None, message=str(exc.detail), hint="")
        response.status_code = exc.status_code
        return response

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> HTMLResponse:
        log.exception("網頁發生未預期的錯誤")
        if isinstance(exc, jinja2.UndefinedError):
            # 畫面（templates/）每次都重新讀檔，程式（.py）卻是開程式時就載入的：更新完程式沒有重開，
            # 就會變成「新的畫面配舊的程式」，畫面要的東西程式還沒給。關掉重開就好，不是資料壞掉。
            return web.page(request, "error.html", None, message="程式更新過了，但這個視窗還在跑舊的版本",
                            hint="請把執行機器人的黑色視窗關掉，再雙擊一次 2-start（資料都沒事）。")
        return web.page(request, "error.html", None, message=f"程式發生未預期的錯誤：{exc!r}",
                        hint="請把 data/church_bot.log 傳給維護的人。")

    # ------------------------------------------------------------------ 首頁：所有牧區

    def ministry_rows() -> list[dict[str, Any]]:
        """首頁每個牧區一列：不連網（不讀服事表），所以首頁一定馬上出來；要處理幾件事由 app.js 問 /m/<編號>/api/nav。"""
        rows = []
        for unit in church.units():
            targets = unit.targets()
            last = unit.service.history.last_run()
            rows.append({
                "id": unit.id, "name": unit.name, "note": unit.ministry.note, "locked": unit.ministry.has_password,
                "groups": sum(1 for t in targets if t.enabled and t.line_id), "groups_total": len(targets),
                "members": len(unit.members()), "schedule": scheduler.status_for(unit.id),
                "next_run": scheduler.next_run_text(unit.id) if scheduler.next_run(unit.id) else "",
                "last": last,
            })
        return rows

    @ui.get("/", response_class=HTMLResponse)
    def home(request: Request, new: str = ""):
        manager_view = web.is_manager(request)
        rows = [{**r, "open": manager_view or web.unlocked(request, r["id"])} for r in ministry_rows()]
        return web.page(request, "church_home.html", None, rows=rows,
                        unassigned=church.unassigned_chats() if manager_view else [],
                        quota=quota_json(church.quota_status()), show_form=new == "1" or not church.ministries())

    @ui.post("/ministries/add")
    async def ministries_add(name: str = Form(""), note: str = Form(""), password: str = Form("")):
        """誰進得了網站都可以新增牧區，但一定要同時設牧區密碼：建的人之後才進得來，別人沒有密碼就進不去。"""
        if len(password.strip()) < MIN_MINISTRY_PASSWORD:
            return redirect("/?new=1", f"請設定這個牧區的密碼（至少 {MIN_MINISTRY_PASSWORD} 個字），之後進這個牧區要用", "error")
        try:
            ministry = await run_in_threadpool(add_ministry, web.paths, name, note)
        except ConfigError as exc:
            return redirect("/?new=1", f"{exc.message}。{exc.hint}".rstrip("。"), "error")
        ministry = edit_ministry(web.paths, ministry.id, password_hash=hash_password(password.strip()))
        await run_in_threadpool(scheduler.reload)
        resp = redirect(f"/m/{ministry.id}/", f"已建立「{ministry.name}」：照下面的運作流程，從服事表開始設定")
        resp.set_cookie(ministry_cookie(ministry.id), ministry_token(ministry.password_hash), httponly=True,
                        samesite="lax", max_age=60 * 60 * 24 * 30)  # 建的人不用馬上再輸入一次
        return resp

    @manager.post("/unassigned/assign")
    def unassigned_assign(chat_id: str = Form(...), ministry_id: str = Form(""), name: str = Form("")):
        if not ministry_id:
            return redirect("/", "請先選要分到哪個牧區", "error")
        target = church.assign_chat(chat_id, ministry_id, name)
        unit = church.require(ministry_id)
        return redirect(f"/m/{unit.id}/targets?edit={quote(target.name)}",
                        f"已把群組分到「{unit.name}」（尚未啟用），確認後勾選「要收到提醒」並儲存")

    @manager.post("/unassigned/forget")
    def unassigned_forget(chat_id: str = Form(...)):
        """不需要的群組（例如測試用、機器人已經退出）：從清單拿掉。機器人之後在那裡被叫到還是會再出現。"""
        church.shared.forget_chat(chat_id)
        return redirect("/", "已從「還沒分配的群組」拿掉")

    # ------------------------------------------------------------------ 全教會設定：LINE 金鑰、網站密碼

    @manager.get("/settings", response_class=HTMLResponse)
    def church_settings(request: Request):
        env = {**read_env_file(web.paths.env_file),
               **{k: v for k, v in os.environ.items() if k.startswith(("LINE_", "UI_"))}}
        try:
            web_settings = load_settings(web.paths).web
        except ConfigError:
            web_settings = None
        return web.page(request, "church_settings.html", None,
                        token_status=mask(env.get("LINE_CHANNEL_ACCESS_TOKEN", "")),
                        secret_status=mask(env.get("LINE_CHANNEL_SECRET", "")),
                        password_status="已設定" if env.get("UI_PASSWORD") else "", web_settings=web_settings,
                        rows=ministry_rows(), local=is_local(request))

    @manager.post("/settings/secrets")
    def settings_secrets(token: str = Form(""), secret: str = Form(""), password: str = Form(""),
                         clear_password: str = Form("")):
        updates = {k: v.strip() for k, v in (("LINE_CHANNEL_ACCESS_TOKEN", token), ("LINE_CHANNEL_SECRET", secret),
                                            ("UI_PASSWORD", password)) if v.strip()}
        if clear_password == "on":
            updates["UI_PASSWORD"] = ""
        if not updates:
            return redirect("/settings", "沒有輸入任何新的金鑰，所以沒有變更", "warn")
        update_env_file(web.paths.env_file, updates)
        return redirect("/settings", "LINE 金鑰 / 密碼已更新（存在 .env，不會上傳到 git）")

    @manager.get("/history", response_class=HTMLResponse)
    def history_page(request: Request):
        return web.history_page(request, None)

    @manager.post("/history/{change_id}/restore")
    async def history_restore(change_id: int):
        msg, level = await run_in_threadpool(web.restore, change_id, "")
        return redirect("/history", msg, level)

    @ui.get("/help", response_class=HTMLResponse)
    def help_page(request: Request):
        first = next(iter(church.ministries()), None)
        return web.page(request, "help.html", None, help_base=f"/m/{first.id}" if first else "")

    # --- LINE「/設定」等待驗證的修改：管理網頁可以直接核准／拒絕（上方那一條，在哪一頁都看得到） ---

    def may_decide(request: Request) -> bool:
        """LINE「/設定」等驗證的修改：伺服器管理員，或能管那個牧區的人，才能核准／拒絕。"""
        pending = webhook.verifier.current()
        return pending is None or web.is_manager(request) or web.unlocked(request, pending.ministry_id)

    @ui.post("/remote-config/approve")
    def remote_config_approve(request: Request, next: str = Form("/")):
        if not may_decide(request):
            return redirect(safe_next(next), "只有能管那個牧區的人可以核准", "error")
        pending = webhook.verifier.take("管理網頁核准")
        if pending is None:
            return redirect(safe_next(next), "這個修改已經過期或被處理掉了", "warn")
        with versions.source("管理網頁（核准 LINE /設定）"):
            webhook.apply(pending)
        where = f"{pending.ministry_name}・" if pending.ministry_name else ""
        return redirect(safe_next(next), f"已核准：{where}{pending.option.key} → {pending.value_text}（LINE 那邊不會另外通知）")

    @ui.post("/remote-config/reject")
    def remote_config_reject(request: Request, next: str = Form("/")):
        if not may_decide(request):
            return redirect(safe_next(next), "只有能管那個牧區的人可以拒絕", "error")
        pending = webhook.verifier.take("管理網頁拒絕")
        if pending is not None:
            log.warning("管理網頁拒絕了 LINE 設定修改：%s・%s → %s（%s）", pending.ministry_name, pending.option.key,
                        pending.value_text, pending.requester or pending.user_id)
        return redirect(safe_next(next), "已拒絕這個修改" if pending else "這個修改已經過期或被處理掉了")

    # ------------------------------------------------------------------ JSON API（整個教會）

    @api.get("/quota", summary="本月 LINE 用量（整個帳號一份；該查的時候會順便向 LINE 問一次）")
    def api_quota(force: bool = False):
        """首頁和主控台畫出來之後由 app.js 問這一支，所以「開著頁面」和「重開網頁」都會看到最新的用量。

        ``force=1`` 是那一格的「⟳」：不管快照多新，一定重新問一次（見 service.refresh_quota）。
        """
        return quota_json(church.refresh_quota(force=force)) or {}

    @api.get("/status", summary="每個牧區的自動發送與上次發送")
    def api_status():
        return {"version": __version__, "schedule": scheduler.status,
                "ministries": [{"id": r["id"], "name": r["name"], "schedule": r["schedule"],
                                "next_run": (n.isoformat() if (n := scheduler.next_run(r["id"])) else None),
                                "last_run": jsonable_encoder(r["last"]) if r["last"] else None}
                               for r in ministry_rows()]}

    # ------------------------------------------------------------------ public endpoints

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"ok": True, "version": __version__}

    @app.post("/line/webhook", include_in_schema=False)
    async def line_webhook(request: Request):
        body = await request.body()
        # LINE 連得到的網址就是「外面看到的網址」：記下來，誰在 LINE 打「/服務網址」就回這一個
        # （cloudflared 會把原本的網址放在 X-Forwarded-Host；簽章驗過才會真的記，見 webhook.handle）
        headers = request.headers
        url = public_base(headers.get("x-forwarded-host") or headers.get("host", ""),
                          headers.get("x-forwarded-proto", "https"))
        try:
            count = await run_in_threadpool(webhook.handle, body, headers.get("x-line-signature", ""), url)
        except SignatureError as exc:
            log.warning("%s", exc)
            return JSONResponse({"ok": False, "error": exc.message}, status_code=400)
        except ChurchBotError as exc:
            log.error("%s", exc)
            return JSONResponse({"ok": False, "error": exc.message}, status_code=503)
        return {"ok": True, "events": count}

    ministry_ui, ministry_api = ministry_routes(web)
    app.include_router(ui)
    app.include_router(manager)
    app.include_router(api)
    app.include_router(ministry_ui)
    app.include_router(ministry_api)
    return app
