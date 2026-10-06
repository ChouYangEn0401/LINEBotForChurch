"""管理網頁（FastAPI + Jinja2，伺服器端產生畫面，不需要任何前端框架或建置步驟）。

頁面照「不熟電腦的同工會怎麼想」分：
* 主控台（現在正不正常、要處理什麼、這週會發什麼）
* 服事表（Google Sheet 的連結 + 把整張表畫出來 + 程式讀到的結果）
* 同工名單（名單本身）→ 子頁「LINE 帳號」「小團」
* LINE 群組、發送紀錄、說明
* 管理員才需要的：設定（一張卡片一個決定）、系統檢查、大教會（實驗）——按右上角「切換身分」才出現

版面跟 Workflow Helper / pyDMS 同一套：側欄每一項底下是它「現在的狀況」（/api/nav），
每一頁開頭是「小標 → 標題 → 一句話」，內容用帶標題列的面板，新增／編輯用右邊的抽屜。
「切換身分」只是把進階的東西收起來（cookie），不是權限；要限制誰能開網頁請設密碼（.env 的 UI_PASSWORD）。
JSON API：/api/*（自動產生的文件在 /docs）。LINE Webhook：/line/webhook（選用）。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import os
import secrets
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import quote

import jinja2
from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from church_bot import __version__
from church_bot.config import (
    DEFAULT_TEMPLATE, SETTINGS_LOCK, WEEKDAY_ZH, Paths, Settings, load_settings, read_env_file, save_settings,
    update_env_file, update_settings,
)
from church_bot.core.accounts import build_accounts
from church_bot.core.dates import format_date
from church_bot.core.directory import Directory, normalize_name, validate_teams
from church_bot.core.planner import DATE_FMT
from church_bot.core.public_url import public_base
from church_bot.core.quota import QuotaSnapshot
from church_bot.core.renderer import Renderer
from church_bot.errors import ChurchBotError, ConfigError
from church_bot.messengers import MESSENGER_KINDS_ZH, build_messenger
from church_bot.models import (
    TRIGGER_ZH, DeliveryStatus, Issue, Member, OutgoingMessage, RunReport, Severity, Target, Team,
)
from church_bot.org import KIND_SUGGESTIONS, OrgReport, OrgTable, OrgUnit, build_org
from church_bot.remote_config import Verifier
from church_bot.scheduler import BotScheduler
from church_bot.service import QUOTA_LOW_THRESHOLD, BotService
from church_bot.sources import SOURCE_KINDS_ZH
from church_bot.sources.google_public import parse_sheet_url
from church_bot.tables import (
    LINE_ID_RE, TABLE_WRITE_LOCK, MemberTable, TargetTable, TeamTable, describe_line_id, fmt_list, parse_list,
    remove, upsert,
)
from church_bot.web.overview import Step, build_steps, issue_link
from church_bot.webhook import SignatureError, WebhookHandler

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
LAYOUTS_ZH = {"auto": "自動判斷（推薦）", "wide": "日期在左、一列一次聚會", "long": "一列一項服事",
              "matrix": "日期在上、一欄一次聚會"}
ADMIN_COOKIE = "church_bot_admin"  # 右上角「切換身分」（只是收起進階畫面，不是權限）


def _safe_next(url: str) -> str:
    """表單帶回來的「回到哪一頁」只能是站內路徑，不能被拿來跳到外部網址。"""
    return url if url.startswith("/") and not url.startswith("//") else "/"


def _redirect(url: str, msg: str = "", level: str = "ok") -> RedirectResponse:
    if msg:
        path, hash_mark, fragment = url.partition("#")  # 訊息參數要放在 #段落 前面，瀏覽器才會送到伺服器
        url = f"{path}{'&' if '?' in path else '?'}msg={quote(msg)}&level={level}{hash_mark}{fragment}"
    return RedirectResponse(url, status_code=303)


def _mask(secret: str) -> str:
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


def form_values(s: Settings) -> dict[str, Any]:
    return {
        "source_kind": s.source.kind, "spreadsheet_url": s.source.spreadsheet_url, "worksheet": s.source.worksheet,
        "csv_path": s.source.csv_path, "credentials_file": s.source.credentials_file, "layout": s.source.layout,
        "ignore_columns": fmt_list(s.source.columns.ignore),
        "messenger_kind": s.messenger.kind, "check_quota": s.messenger.check_quota,
        "schedule_enabled": s.schedule.enabled, "day_of_week": s.schedule.day_of_week, "time": s.schedule.time,
        "timezone": s.schedule.timezone, "every_n_weeks": s.schedule.every_n_weeks,
        "title": s.message.title, "footer": s.message.footer, "date_format": s.message.date_format,
        "template": s.message.template, "role_order": fmt_list(s.message.role_order),
        "name_separator": s.message.name_separator,
        "admin_target_id": s.line.admin_target_id,
        "lookahead_days": s.behavior.lookahead_days, "resend_if_changed": s.behavior.resend_if_changed,
        "roster_low_warning_days": s.behavior.roster_low_warning_days,
        "warn_unknown_names": s.behavior.warn_unknown_names,
        "collect_names": s.chat.collect_names, "remote_config": s.chat.remote_config,
        "send_code_to_admin": s.chat.send_code_to_admin,
    }


def apply_form(current: Settings, f: dict[str, str]) -> Settings:
    """把設定頁送出的表單套到目前設定上；任何不合法的值都丟 ConfigError（中文說明）。"""
    data = current.model_dump()
    on = lambda key: f.get(key) == "on"  # noqa: E731 - checkbox 沒勾就不會出現在表單裡
    try:
        lookahead, low_days = int(f.get("lookahead_days", "7")), int(f.get("roster_low_warning_days", "14"))
        every_n_weeks = int(f.get("every_n_weeks", "1"))
    except ValueError as exc:
        raise ConfigError("「往後看幾天」「剩幾天提醒」「每幾週發送一次」要填數字") from exc
    data["source"].update(
        kind=f.get("source_kind", "csv"), spreadsheet_url=f.get("spreadsheet_url", "").strip(),
        worksheet=f.get("worksheet", "").strip(), csv_path=f.get("csv_path", "").strip(),
        credentials_file=f.get("credentials_file", "").strip() or "config/service-account.json",
        layout=f.get("layout", "auto"),
    )
    data["source"]["columns"]["ignore"] = list(parse_list(f.get("ignore_columns", "")))
    data["messenger"].update(kind=f.get("messenger_kind", "line"), check_quota=on("check_quota"))
    data["schedule"].update(enabled=on("schedule_enabled"), day_of_week=f.get("day_of_week", "sat"),
                            time=f.get("time", "20:00"), timezone=f.get("timezone", "Asia/Taipei").strip(),
                            every_n_weeks=every_n_weeks)
    data["message"].update(
        title=f.get("title", "").strip(), footer=f.get("footer", "").strip(),
        date_format=f.get("date_format", "").strip() or "%-m/%-d（{weekday}）",
        template=f.get("template", "").replace("\r\n", "\n") or DEFAULT_TEMPLATE,
        role_order=list(parse_list(f.get("role_order", ""))), name_separator=f.get("name_separator") or "、",
    )
    data["line"]["admin_target_id"] = f.get("admin_target_id", "").strip()
    data["behavior"].update(lookahead_days=lookahead, resend_if_changed=on("resend_if_changed"),
                            roster_low_warning_days=low_days, warn_unknown_names=on("warn_unknown_names"))
    data["chat"].update(collect_names=on("collect_names"), remote_config=on("remote_config"),
                        send_code_to_admin=on("send_code_to_admin"))
    try:
        new = Settings.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        raise ConfigError(f"「{'.'.join(str(x) for x in first['loc'])}」{first['msg']}") from exc

    admin = new.line.admin_target_id
    if admin and not LINE_ID_RE.match(admin):
        raise ConfigError("管理員 LINE ID 格式不對", "要是 U 或 C 開頭再加 32 個英數字。私訊機器人「/我的ID」就能拿到。")
    if new.source.kind in ("google_public", "google_service_account"):
        parse_sheet_url(new.source.spreadsheet_url)
    Renderer(new.message).validate()
    return new


def create_app(paths: Paths) -> FastAPI:
    service = BotService(paths)
    scheduler = BotScheduler(service)
    webhook = WebhookHandler(service, Verifier(), on_settings_changed=scheduler.reload)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    asset_hash = hashlib.sha1()
    for static in sorted((HERE / "static").glob("*")):
        asset_hash.update(static.read_bytes())
    templates.env.globals.update(
        version=__version__, asset_v=asset_hash.hexdigest()[:10], weekday_zh=WEEKDAY_ZH, describe_line_id=describe_line_id, fmt_list=fmt_list,
        status_zh={s.value: s.zh for s in DeliveryStatus}, trigger_zh=TRIGGER_ZH,
        source_kinds=SOURCE_KINDS_ZH, messenger_kinds=MESSENGER_KINDS_ZH, layouts=LAYOUTS_ZH, issue_link=issue_link,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        scheduler.start()
        try:
            yield
        finally:
            scheduler.shutdown()

    app = FastAPI(title="教會服事提醒機器人", version=__version__, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    app.state.webhook = webhook

    SESSION_COOKIE = "church_bot_session"

    def current_password() -> str:
        return os.environ.get("UI_PASSWORD") or read_env_file(paths.env_file).get("UI_PASSWORD", "")

    def session_token(password: str) -> str:
        return hashlib.sha256(password.encode()).hexdigest()

    class LoginRequired(Exception):
        pass

    def require_login(request: Request) -> None:
        """沒設密碼＝直接放行；設了密碼＝一定要有對得上目前密碼的 session cookie，不然一律擋下來、
        直接顯示登入畫面（不用跳轉），改密碼後舊的 cookie 立刻對不上、所有人都要重新登入。"""
        password = current_password()
        if not password:
            return
        if secrets.compare_digest(request.cookies.get(SESSION_COOKIE, ""), session_token(password)):
            return
        raise LoginRequired()

    ui = APIRouter(dependencies=[Depends(require_login)])
    api = APIRouter(prefix="/api", tags=["API"], dependencies=[Depends(require_login)])

    def page(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        ctx.setdefault("flash", request.query_params.get("msg", ""))
        ctx.setdefault("flash_level", request.query_params.get("level", "ok"))
        pending = webhook.verifier.current()
        today = dt.date.today()
        next_run = scheduler.next_run_text()
        # 內建排程關著 = 由 Telegram 來呼叫 cli.bat send；對看畫面的人來說這不是「關閉」，是「別人在排」
        schedule_text = scheduler.status if next_run != "（沒有排程）" else "由 Telegram 排程發送"
        base = {"nav": name.removesuffix(".html"), "schedule_status": scheduler.status, "schedule_text": schedule_text,
                "next_run": next_run, "has_password": bool(current_password()),
                "pending_change": pending,
                "pending_minutes": webhook.verifier.minutes_left(pending) if pending else 0,
                "admin_mode": request.cookies.get(ADMIN_COOKIE) == "1", "current_path": request.url.path,
                "today_text": f"{today.isoformat()} · 週{'一二三四五六日'[today.weekday()]}"}
        return templates.TemplateResponse(request, name, {**base, **ctx})

    @ui.post("/admin-mode")
    def admin_mode(enabled: str = Form(""), next: str = Form("/")):
        """右上角「切換身分」：切到管理員 = 側欄多出設定、系統檢查、實驗，畫面上多出 ID、檔案位置這類細節。"""
        on = enabled == "1"
        resp = _redirect(_safe_next(next), "已切到管理員：側欄多了設定、系統檢查" if on else "已回到一般畫面")
        if on:
            resp.set_cookie(ADMIN_COOKIE, "1", samesite="lax", max_age=60 * 60 * 24 * 365)
        else:
            resp.delete_cookie(ADMIN_COOKIE)
        return resp

    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, exc: LoginRequired) -> HTMLResponse:
        next_url = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return templates.TemplateResponse(request, "login.html", {"next": next_url}, status_code=401)

    @app.post("/login")
    async def login(request: Request, password: str = Form(""), next: str = Form("/")):
        if not (next.startswith("/") and not next.startswith("//")):
            next = "/"  # next 是網址列裡的路徑，不能是外部網址，避免被拿來做開放重導向釣魚
        saved = current_password()
        if saved and secrets.compare_digest(password, saved):
            resp = RedirectResponse(next or "/", status_code=303)
            resp.set_cookie(SESSION_COOKIE, session_token(saved), httponly=True, samesite="lax",
                            max_age=60 * 60 * 24 * 30)
            return resp
        return templates.TemplateResponse(request, "login.html", {"next": next, "error": "密碼不對，再試一次"},
                                          status_code=401)

    @app.post("/logout")
    def logout():
        resp = _redirect("/")
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    @app.exception_handler(ChurchBotError)
    async def friendly_error(request: Request, exc: ChurchBotError) -> HTMLResponse:
        return page(request, "error.html", message=exc.message, hint=exc.hint)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> HTMLResponse:
        log.exception("網頁發生未預期的錯誤")
        if isinstance(exc, jinja2.UndefinedError):
            # 畫面（templates/）每次都重新讀檔，程式（.py）卻是開程式時就載入的：更新完程式沒有重開，
            # 就會變成「新的畫面配舊的程式」，畫面要的東西程式還沒給。關掉重開就好，不是資料壞掉。
            return page(request, "error.html", message="程式更新過了，但這個視窗還在跑舊的版本",
                        hint="請把執行機器人的黑色視窗關掉，再雙擊一次 2-start（資料都沒事）。")
        return page(request, "error.html", message=f"程式發生未預期的錯誤：{exc!r}",
                    hint="請把 data/church_bot.log 傳給維護的人。")

    # ------------------------------------------------------------------ dashboard

    @ui.get("/", response_class=HTMLResponse)
    def index(request: Request):
        report, plan = service.preview()
        return page(
            request, "index.html", report=report, plan=plan, last=service.history.last_run(),
            problems=[i for i in report.issues if i.severity is not Severity.INFO],
            infos=[i for i in report.issues if i.severity is Severity.INFO],
            steps=workflow_steps(report), quota=quota_json(service.quota_status()),
        )

    def quota_json(snapshot: QuotaSnapshot | None) -> dict[str, Any] | None:
        """本月用量那一格要顯示的東西。None = 不適用（測試模式、關掉額度檢查），那一格就不出現。

        畫面和 /api/quota 共用同一份：app.js 收到新的就直接換掉那一格的字，不用重新整理。
        """
        if snapshot is None:
            return None
        remaining = snapshot.remaining
        return {"value": snapshot.value_text(), "detail": snapshot.detail_text(dt.datetime.now().astimezone()),
                "low": remaining is not None and remaining < QUOTA_LOW_THRESHOLD,
                "stale": bool(snapshot.error) or not snapshot.known}

    @api.get("/quota", summary="本月 LINE 用量（該查的時候會順便向 LINE 問一次）")
    def api_quota(force: bool = False):
        """主控台畫出來之後由 app.js 問這一支，所以「開著頁面」和「重開網頁」都會看到最新的用量。

        ``force=1`` 是那一格的「⟳」：不管快照多新，一定重新問一次（見 service.refresh_quota）。
        """
        return quota_json(service.refresh_quota(force=force)) or {}

    def workflow_steps(report: RunReport) -> list[Step]:
        try:
            ctx = service.load()
        except ChurchBotError:
            return []  # 設定檔壞了：上面的「需要處理的事項」已經會說明
        roster, roster_error = None, ""
        try:
            roster = service.fetch_roster(ctx.settings, service.now(ctx.settings).date(), use_cache=True)
        except ChurchBotError as exc:
            roster_error = exc.message
        accounts = build_accounts(service.history.people(), ctx.members)
        unknown = sum(1 for i in report.issues if i.code == "unknown_name")
        return build_steps(issues=report.issues, settings=ctx.settings, roster=roster, roster_error=roster_error,
                           members=ctx.members, targets=ctx.targets, unknown_names=unknown,
                           pending_claims=sum(1 for a in accounts if a.needs_review and not a.ignored),
                           next_run=scheduler.next_run_text())

    @api.get("/nav", summary="側欄每一項現在的狀況（那行小字和右邊的數字）")
    def api_nav():
        """側欄不只是選單：每一項底下寫的是「它現在怎麼樣」，有事要處理的會標數字。

        頁面先畫出來、再由 app.js 來問這一支，所以讀 Google Sheet 慢的時候不會卡住整頁。
        """
        tone = {"ok": "ok", "warning": "warn", "error": "bad", "off": ""}
        report, _plan = service.preview()
        problems = [i for i in report.issues if i.severity is not Severity.INFO]
        errors = [i for i in problems if i.is_error]
        status: dict[str, dict[str, Any]] = {"index": {
            "sub": f"{len(errors)} 個問題會擋住發送" if errors else
                   f"{len(problems)} 件事要看一下" if problems else "一切正常",
            "count": len(problems), "tone": "bad" if errors else "warn" if problems else "ok",
        }}
        for step in workflow_steps(report):
            key = {"服事表": "roster", "同工名單": "members", "LINE 群組": "targets"}.get(step.title)
            if key:
                status[key] = {"sub": step.summary, "count": 0, "tone": tone[step.status]}
        if "members" in status:
            try:
                unknown = len(service.unknown_names())
            except ChurchBotError:
                unknown = 0
            members = MemberTable(paths.members_file).load().items
            pending = sum(1 for a in build_accounts(service.history.people(), members)
                          if a.needs_review and not a.ignored)
            parts = [f"{len(members)} 位" if members else "還沒設定"]
            parts += [f"{unknown} 個名字對不到"] if unknown else []
            parts += [f"{pending} 位待確認"] if pending else []
            status["members"].update(sub=" · ".join(parts), count=unknown + pending)
        last = service.history.last_run()
        status["runs"] = ({"sub": f"上次 {last.started_at[5:16].replace('-', '/').replace('T', ' ')} · {last.status_zh}",
                           "count": 0, "tone": tone.get(last.status, "")} if last
                          else {"sub": "還沒有發送過", "count": 0, "tone": ""})
        status["settings"] = {"sub": scheduler.status if scheduler.next_run() else "由 Telegram 排程發送",
                              "count": 0, "tone": ""}
        return status

    @ui.post("/send")
    def send(force: int = Form(0)):
        report, _ = service.run("manual", force=bool(force))
        msg, level = run_summary(report)
        return _redirect(f"/runs/{report.run_id}", msg, level)

    @ui.get("/check", response_class=HTMLResponse)
    def check(request: Request):
        return page(request, "check.html", items=service.health())

    # ------------------------------------------------------------------ 服事表（Google Sheet）

    def _parsed_rows(roster, directory: Directory, today: dt.date, window_end: dt.date) -> tuple[list[str], list[dict]]:
        """把 Roster 攤成「程式讀到的結果」表格：一列一次聚會、一欄一項服事，名字對不到的標起來。"""
        roles = roster.all_roles()
        rows: list[dict] = []
        for day in roster.days:
            cells: dict[str, list[tuple[str, bool]]] = {}
            for a in day.assignments:
                cells[a.role] = [(directory.resolve(n).display if directory.resolve(n).matched else n,
                                  directory.resolve(n).matched or directory.is_empty) for n in a.names]
            rows.append({"date": format_date(day.date, DATE_FMT), "iso": day.date.isoformat(), "label": day.label,
                         "note": day.note, "cells": cells, "current": today <= day.date <= window_end,
                         "past": day.date < today})
        return roles, rows

    @ui.get("/roster", response_class=HTMLResponse)
    def roster_page(request: Request):
        ctx = None
        roster, roster_error, roster_hint = None, "", ""
        roles: list[str] = []
        parsed: list[dict] = []
        today = dt.date.today()
        window_end = today
        try:
            ctx = service.load()
            today = service.now(ctx.settings).date()
            window_end = today + dt.timedelta(days=ctx.settings.behavior.lookahead_days - 1)
            roster = service.fetch_roster(ctx.settings, today, use_cache=True)
            roles, parsed = _parsed_rows(roster, ctx.directory, today, window_end)
        except ChurchBotError as exc:
            roster_error, roster_hint = exc.message, exc.hint
        source = ctx.settings.source if ctx else None
        sheet_url = source.spreadsheet_url if source and source.kind.startswith("google") else ""
        return page(
            request, "roster.html", roster=roster, roster_error=roster_error, roster_hint=roster_hint,
            source=source, sheet_url=sheet_url, roles=roles, parsed=parsed, today=today, window_end=window_end,
            issues=[i for i in (roster.issues if roster else ()) if i.severity is not Severity.INFO],
            infos=[i for i in (roster.issues if roster else ()) if i.severity is Severity.INFO],
            last_date=format_date(roster.last_date, "%Y/%-m/%-d") if roster and roster.last_date else "",
        )

    @ui.post("/roster/refresh")
    def roster_refresh():
        service.clear_roster_cache()
        return _redirect("/roster", "已重新讀取服事表")

    @ui.post("/roster/source")
    def roster_source(url: str = Form("")):
        """「服事表」頁最簡單的接法：貼 Google Sheet 網址 → 先試讀一次 → 讀得到才存。"""
        url = url.strip()
        if not url:
            return _redirect("/roster", "請先貼上 Google Sheet 的網址", "error")
        try:
            parse_sheet_url(url)
        except ChurchBotError as exc:
            return _redirect("/roster", f"{exc.message}。{exc.hint}", "error")
        with SETTINGS_LOCK:
            trial = load_settings(paths).model_copy(deep=True)
            trial.source.kind, trial.source.spreadsheet_url, trial.source.worksheet = "google_public", url, ""
            try:
                roster = service.fetch_roster(trial, service.now(trial).date())
            except ChurchBotError as exc:
                return _redirect("/roster", f"還沒換過去，因為讀不到這份表：{exc.message}。{exc.hint}", "error")
            save_settings(paths, trial)
        last = format_date(roster.last_date, "%Y/%-m/%-d") if roster.last_date else "（沒有日期）"
        return _redirect("/roster", f"已接上：{roster.source}，共 {len(roster.days)} 次聚會，排到 {last}")

    @api.get("/roster/sheet", summary="服事表的原始格子（給「服事表」頁的表格檢視用）")
    def api_roster_sheet():
        try:
            ctx, sheets, infos, today = service.sheet_preview()
        except ChurchBotError as exc:
            return {"ok": False, "error": exc.message, "hint": exc.hint}
        try:
            unknown = list(service.unknown_names())
        except ChurchBotError:
            unknown = []
        window_end = today + dt.timedelta(days=ctx.settings.behavior.lookahead_days - 1)
        return {
            "ok": True, "today": today.isoformat(), "window_end": window_end.isoformat(), "unknown_names": unknown,
            "sheets": [{
                "label": sheet.source, "rows": sheet.rows,
                "layout": info.layout if info else "", "header_row": info.header_row if info else -1,
                "date_axis": info.date_axis if info else "", "cols": info.cols if info else {},
                "dates": {str(k): v.isoformat() for k, v in info.dates.items()} if info else {},
            } for sheet, info in zip(sheets, infos)],
        }

    # ------------------------------------------------------------------ targets

    def targets_response(request: Request, editing: Target | None = None, new: bool = False,
                         original_name: str | None = None, error: str = "") -> HTMLResponse:
        result = TargetTable(paths.targets_file).load()
        known = {t.line_id for t in result.items}
        chats = [c for c in service.history.chats()
                 if c["chat_id"] not in known and c["status"] == "active" and c["kind"] in ("group", "room")]
        show_form = bool(editing) or new  # 清單和表單分開兩個畫面：一次只看一件事
        try:
            message = load_settings(paths).message
        except ConfigError:
            message = Settings().message
        preview, preview_note = service.preview_target(editing) if editing and not error else ([], "")
        extra = {"flash": error, "flash_level": "error"} if error else {}
        return page(request, "targets.html", targets=result.items, issues=result.issues, chats=chats, editing=editing,
                    show_form=show_form, roles_in_sheet=service.roster_roles() if show_form else [],
                    original_name=editing.name if original_name is None and editing else (original_name or ""),
                    message_defaults=message, preview=preview, preview_note=preview_note, **extra)

    @ui.get("/targets", response_class=HTMLResponse)
    def targets_page(request: Request, edit: str = "", new: str = ""):
        editing = next((t for t in TargetTable(paths.targets_file).load().items if t.name == edit), None)
        return targets_response(request, editing, new == "1")

    @ui.post("/targets/save")
    def targets_save(request: Request, name: str = Form(""), line_id: str = Form(""), enabled: str = Form(""),
                     roles: str = Form(""), labels: str = Form(""), mention: str = Form(""), note: str = Form(""),
                     title: str = Form(""), footer: str = Form(""), template: str = Form(""),
                     original_name: str = Form("")):
        name, line_id = name.strip(), line_id.strip()
        target = Target(name=name, line_id=line_id, enabled=enabled == "on", roles=parse_list(roles),
                        labels=parse_list(labels), mention=mention == "on", note=note.strip(), title=title.strip(),
                        footer=footer.strip(), template=template.replace("\r\n", "\n").strip())
        # 錯誤時直接把表單畫回去（不跳轉），剛打好的訊息模板才不會不見
        if not name:
            return targets_response(request, target, original_name=original_name, error="請填群組名稱")
        if line_id and not LINE_ID_RE.match(line_id):
            return targets_response(request, target, original_name=original_name,
                                    error=f"LINE_ID 格式不對：「{line_id}」。要是 C 或 U 開頭再加 32 個英數字")
        try:
            Renderer(load_settings(paths).message).validate(target)
        except ChurchBotError as exc:
            return targets_response(request, target, original_name=original_name,
                                    error=f"還沒儲存：{exc.message}。{exc.hint}")
        with TABLE_WRITE_LOCK:
            table = TargetTable(paths.targets_file)
            table.save(upsert(table.load().items, target, key=lambda t: t.name, original_key=original_name or None))
        if target.enabled and not line_id:
            return _redirect("/targets", f"已儲存「{name}」，但還沒填 LINE_ID，所以不會收到提醒", "warn")
        if target.has_own_message:  # 改了訊息：留在編輯畫面，下面就是這個群組現在會收到的樣子
            return _redirect(f"/targets?edit={quote(name)}#preview", f"已儲存「{name}」，下面是這個群組現在會收到的訊息")
        return _redirect("/targets", f"已儲存「{name}」")

    @ui.post("/targets/delete")
    def targets_delete(name: str = Form(...)):
        with TABLE_WRITE_LOCK:
            table = TargetTable(paths.targets_file)
            table.save(remove(table.load().items, name, key=lambda t: t.name))
        return _redirect("/targets", f"已刪除「{name}」")

    @ui.post("/targets/add-chat")
    def targets_add_chat(chat_id: str = Form(...), name: str = Form("")):
        name = name or f"新群組 {dt.date.today():%m/%d}"
        with TABLE_WRITE_LOCK:
            table = TargetTable(paths.targets_file)
            items = table.load().items
            if existing := next((t for t in items if t.line_id == chat_id), None):
                name = existing.name
            else:
                items.append(Target(name=name, line_id=chat_id, enabled=False))
                table.save(items)
        return _redirect(f"/targets?edit={quote(name)}", "已加入 LINE 群組（尚未啟用），確認後勾選「啟用」並儲存")

    @ui.post("/targets/test")
    def targets_test(name: str = Form(...)):
        target = next((t for t in TargetTable(paths.targets_file).load().items if t.name == name), None)
        if target is None or not LINE_ID_RE.match(target.line_id):
            return _redirect("/targets", f"「{name}」沒有正確的 LINE_ID，無法測試", "error")
        messenger = build_messenger(load_settings(paths), paths)
        try:
            messenger.send(target.line_id, OutgoingMessage(
                text=f"✅ 測試訊息：服事提醒機器人已經可以送訊息到「{target.name}」了！\n之後會在這裡自動提醒每週的服事 🙏"))
        finally:
            messenger.close()
        return _redirect("/targets", f"已送出測試訊息到「{name}」，請到 LINE 確認有沒有收到")

    # ------------------------------------------------------------------ members

    @ui.get("/members", response_class=HTMLResponse)
    def members_page(request: Request, edit: str = "", new: str = ""):
        """名單本身。LINE 帳號、小團各自有子頁，這一頁只做「誰在服事、名字怎麼寫」。"""
        result = MemberTable(paths.members_file).load()
        try:
            unknown, unknown_error = service.unknown_names(), ""
        except ChurchBotError as exc:
            unknown, unknown_error = {}, f"{exc.message}（{exc.hint}）" if exc.hint else exc.message
        accounts = build_accounts(service.history.people(), result.items)
        editing = next((m for m in result.items if m.name == edit), None)
        return page(request, "members.html", members=result.items, issues=result.issues, unknown=unknown,
                    unknown_error=unknown_error, editing=editing, show_form=bool(editing) or new == "1",
                    line_names={a.user_id: a.display_name for a in accounts},
                    pending_accounts=sum(1 for a in accounts if a.needs_review and not a.ignored))

    @ui.get("/members/accounts", response_class=HTMLResponse)
    def members_accounts_page(request: Request):
        """LINE 帳號 ↔ 同工名單：誰登記了名字等確認、哪個帳號是名單上的誰。"""
        result = MemberTable(paths.members_file).load()
        try:
            collect_names = load_settings(paths).chat.collect_names
        except ConfigError:
            collect_names = False
        accounts = build_accounts(service.history.people(), result.items)
        return page(request, "members_accounts.html", members=result.items, collect_names=collect_names,
                    accounts=[a for a in accounts if not a.ignored],
                    ignored_accounts=[a for a in accounts if a.ignored])

    @ui.get("/members/teams", response_class=HTMLResponse)
    def members_teams_page(request: Request, edit_team: str = "", new: str = ""):
        """小團：服事表寫團名，提醒就列出成員。"""
        result = MemberTable(paths.members_file).load()
        teams = TeamTable(paths.teams_file).load()
        editing_team = next((t for t in teams.items if t.name == edit_team), None)
        return page(request, "members_teams.html", members=result.items, teams=teams.items, editing_team=editing_team,
                    show_form=bool(editing_team) or new == "1",
                    team_issues=[*teams.issues, *validate_teams(teams.items, result.items)])

    @ui.post("/members/save")
    def members_save(name: str = Form(""), aliases: str = Form(""), line_user_id: str = Form(""),
                     active: str = Form(""), note: str = Form(""), original_name: str = Form(""), admin: str = Form("")):
        name, uid = name.strip(), line_user_id.strip()
        if not name:
            return _redirect("/members", "請填名字", "error")
        if uid and not (LINE_ID_RE.match(uid) and uid.startswith("U")):
            return _redirect(f"/members?edit={quote(original_name)}",
                             f"LINE_userId 格式不對：「{uid}」。要是 U 開頭再加 32 個英數字", "error")
        member = Member(name=name, aliases=parse_list(aliases), line_user_id=uid, active=active == "on", note=note.strip(),
                        admin=admin == "on")
        with TABLE_WRITE_LOCK:
            table = MemberTable(paths.members_file)
            table.save(upsert(table.load().items, member, key=lambda m: m.name, original_key=original_name or None))
        return _redirect("/members", f"已儲存「{name}」")

    @ui.post("/members/admin")
    def members_admin(name: str = Form(...), enabled: str = Form(""), next_url: str = Form("/members", alias="next")):
        """把某位同工勾成（或取消）管理員。能開管理網頁的人本來就能改所有設定，所以不用另外驗證。"""
        on = enabled == "1"
        with TABLE_WRITE_LOCK:
            table = MemberTable(paths.members_file)
            items = table.load().items
            member = next((m for m in items if m.name == name), None)
            if member is None:
                return _redirect(_safe_next(next_url), f"同工名單裡找不到「{name}」", "error")
            table.save([replace(m, admin=on) if m is member else m for m in items])
        if on and not member.line_user_id:
            return _redirect(_safe_next(next_url), f"「{name}」已經是管理員，但還沒對應 LINE 帳號，所以在 LINE 上還認不出他", "warn")
        return _redirect(_safe_next(next_url), f"「{name}」{'已經是管理員了' if on else '不再是管理員'}")

    @ui.post("/members/delete")
    def members_delete(name: str = Form(...)):
        with TABLE_WRITE_LOCK:
            table = MemberTable(paths.members_file)
            table.save(remove(table.load().items, name, key=lambda m: m.name))
        return _redirect("/members", f"已刪除「{name}」")

    # --- 其他寫法（一個人可以有很多個稱呼，一次加一個 / 刪一個） ---

    def _change_aliases(name: str, change, fragment: str = ""):  # noqa: ANN001
        """把某位同工的「其他寫法」換成 change(他, 全部同工) 算出來的結果。

        ``change`` 回傳 (新的其他寫法, 不行的原因)；有原因就不存檔。
        這裡回傳「要給瀏覽器的錯誤畫面」或 None（成功）。
        """
        with TABLE_WRITE_LOCK:
            table = MemberTable(paths.members_file)
            items = table.load().items
            member = next((m for m in items if m.name == name), None)
            if member is None:
                return _redirect(f"/members{fragment}", f"同工名單裡找不到「{name}」", "error")
            aliases, problem = change(member, items)
            if problem:
                return _redirect(f"/members{fragment}", problem, "error")
            table.save([replace(m, aliases=aliases) if m is member else m for m in items])
        return None

    @ui.post("/members/aliases/add")
    def alias_add(name: str = Form(...), alias: str = Form(...)):
        alias = alias.strip()
        if not alias:
            return _redirect("/members", "請填要新增的寫法", "error")

        def change(member: Member, items: list[Member]):
            if any(normalize_name(a) == normalize_name(alias) for a in (member.name, *member.aliases)):
                return member.aliases, f"「{alias}」已經是「{member.name}」的寫法了"
            others = [m for m in items if m is not member]
            if (owner := Directory(others).lookup(alias)) is not None:
                return member.aliases, f"「{alias}」已經是同工「{owner.name}」的名字或其他寫法，不能重複"
            return (*member.aliases, alias), ""

        if bad := _change_aliases(name, change):
            return bad
        return _redirect("/members", f"已把「{alias}」加成「{name}」的其他寫法")

    @ui.post("/members/aliases/remove")
    def alias_remove(name: str = Form(...), alias: str = Form(...)):
        def change(member: Member, _items: list[Member]):
            return tuple(a for a in member.aliases if a != alias), ""

        if bad := _change_aliases(name, change):
            return bad
        return _redirect("/members", f"已把「{alias}」從「{name}」的其他寫法移除")

    # --- LINE 帳號 ↔ 同工名單（按鈕說明見 core/accounts.py） ---

    @ui.post("/members/accounts/add")
    def account_add(user_id: str = Form(...)):
        person = service.history.person(user_id)
        if person is None:
            return _redirect("/members/accounts", "找不到這個 LINE 帳號（可能已經刪掉了）", "error")
        claimed = person["real_name"]
        name = claimed or person["display_name"] or f"新朋友 {user_id[-6:]}"
        with TABLE_WRITE_LOCK:
            table = MemberTable(paths.members_file)
            items = table.load().items
            if any(m.line_user_id == user_id for m in items):
                return _redirect("/members/accounts", "這個 LINE 帳號已經在同工名單裡了", "warn")
            if (same := Directory(items).lookup(name)) is not None:
                return _redirect("/members/accounts", f"同工名單已經有「{same.name}」（名字或其他寫法是「{name}」），"
                                                      "是同一個人的話請按「對應」", "error")
            line_name = f"LINE 名稱：{person['display_name']}；" if person["display_name"] else ""
            todo = "" if claimed else "，確認真實姓名後改名並勾選「還在服事」"
            items.append(Member(name=name, line_user_id=user_id, active=bool(claimed),
                                note=f"{line_name}{dt.date.today():%m/%d} 從 LINE 帳號加入{todo}"))
            table.save(items)
        service.history.clear_real_name(user_id)
        if claimed:
            return _redirect("/members/accounts", f"已把「{name}」加進同工名單")
        return _redirect(f"/members?edit={quote(name)}", f"已加入「{name}」（先停用），請把名字改成真實姓名再啟用")

    @ui.post("/members/accounts/link")
    def account_link(user_id: str = Form(...), member_name: str = Form(...)):
        with TABLE_WRITE_LOCK:
            table = MemberTable(paths.members_file)
            items = table.load().items
            target = next((m for m in items if m.name == member_name), None)
            if any(m.line_user_id == user_id for m in items):
                return _redirect("/members/accounts", "這個 LINE 帳號已經對應到同工了", "warn")
            if target is None:
                return _redirect("/members/accounts", f"同工名單裡找不到「{member_name}」", "error")
            if target.line_user_id:
                return _redirect("/members/accounts", f"「{member_name}」已經對應到另一個 LINE 帳號，請先確認是不是同一個人",
                                 "error")
            table.save([replace(m, line_user_id=user_id) if m is target else m for m in items])
        service.history.clear_real_name(user_id)
        return _redirect("/members/accounts", f"已把 LINE 帳號對應到「{member_name}」")

    @ui.post("/members/accounts/rename")
    def account_rename(user_id: str = Form(...)):
        new_name = (service.history.person(user_id) or {}).get("real_name", "")
        if not new_name:
            return _redirect("/members/accounts", "這個帳號沒有登記新的名字", "warn")
        with TABLE_WRITE_LOCK:
            table = MemberTable(paths.members_file)
            items = table.load().items
            current = next((m for m in items if m.line_user_id == user_id), None)
            if current is None:
                return _redirect("/members/accounts", "這個 LINE 帳號還沒對應到同工，請用「加入」或「對應」", "warn")
            if (other := Directory([m for m in items if m is not current]).lookup(new_name)) is not None:
                return _redirect("/members/accounts", f"「{new_name}」已經是同工「{other.name}」的名字或其他寫法，"
                                                      "請先確認是不是同一個人", "error")
            # 舊名字留在「其他寫法」：服事表還沒改過來的地方一樣對得到
            aliases = tuple(a for a in dict.fromkeys((*current.aliases, current.name))
                            if normalize_name(a) != normalize_name(new_name))
            table.save([replace(m, name=new_name, aliases=aliases) if m is current else m for m in items])
        service.history.clear_real_name(user_id)
        return _redirect("/members/accounts", f"已把「{current.name}」改名成「{new_name}」（舊名字留在「其他寫法」）")

    @ui.post("/members/accounts/nickname")
    def account_nickname(user_id: str = Form(...), nickname: str = Form(...)):
        """本人用「/我的暱稱」登記的稱呼：加進他對應的同工的「其他寫法」。"""
        with TABLE_WRITE_LOCK:
            table = MemberTable(paths.members_file)
            items = table.load().items
            member = next((m for m in items if m.line_user_id == user_id), None)
            if member is None:
                return _redirect("/members/accounts", "這個 LINE 帳號還沒對應到同工，請先按「加入」或「對應」", "warn")
            if (owner := Directory([m for m in items if m is not member]).lookup(nickname)) is not None:
                return _redirect("/members/accounts", f"「{nickname}」已經是同工「{owner.name}」的名字或其他寫法，"
                                                     "請先確認是不是同一個人", "error")
            if not any(normalize_name(a) == normalize_name(nickname) for a in (member.name, *member.aliases)):
                table.save([replace(m, aliases=(*m.aliases, nickname)) if m is member else m for m in items])
        service.history.drop_nickname(user_id, nickname)
        return _redirect("/members/accounts", f"已把「{nickname}」加成「{member.name}」的其他寫法")

    @ui.post("/members/accounts/nickname/drop")
    def account_nickname_drop(user_id: str = Form(...), nickname: str = Form(...)):
        service.history.drop_nickname(user_id, nickname)
        return _redirect("/members/accounts", f"已忽略登記的暱稱「{nickname}」（同工名單不變）")

    @ui.post("/members/accounts/ignore")
    def account_ignore(user_id: str = Form(...)):
        service.history.clear_real_name(user_id)
        service.history.drop_nickname(user_id)  # 登記的暱稱一起放掉，不然還是會一直排在待處理
        linked = any(m.line_user_id == user_id for m in MemberTable(paths.members_file).load().items)
        if linked:  # 已經是同工：只是不採用這次登記的名字，帳號本身照樣列出來
            return _redirect("/members/accounts", "已略過這次登記的名字，同工名單不變")
        service.history.set_person_ignored(user_id, True)
        return _redirect("/members/accounts", "已忽略這個帳號（本人重新登記名字時才會再出現）")

    @ui.post("/members/accounts/unignore")
    def account_unignore(user_id: str = Form(...)):
        service.history.set_person_ignored(user_id, False)
        return _redirect("/members/accounts", "已取消忽略")

    @ui.post("/members/accounts/forget")
    def account_forget(user_id: str = Form(...)):
        service.history.forget_person(user_id)
        return _redirect("/members/accounts", "已刪除這個帳號的紀錄（他之後在群組講話還是會再被記下來）")

    @ui.post("/members/collect")
    def members_collect(enabled: str = Form("")):
        on = enabled == "1"

        def change(settings: Settings) -> None:
            settings.chat.collect_names = on

        update_settings(paths, change)
        if on:
            return _redirect("/members/accounts", "已開放名字登記：請大家在 LINE 打「/我的名字 真實姓名」。收集完記得關掉")
        return _redirect("/members/accounts", "已關閉名字登記")

    @ui.post("/members/alias")
    def members_alias(raw: str = Form(...), member_name: str = Form("")):
        raw = raw.strip()
        with TABLE_WRITE_LOCK:
            table = MemberTable(paths.members_file)
            items = table.load().items
            if not member_name:  # 新增成一位新同工
                items = upsert(items, Member(name=raw), key=lambda m: m.name)
                msg = f"已新增同工「{raw}」"
            else:
                items = [replace(m, aliases=(*m.aliases, raw) if raw not in m.aliases else m.aliases)
                         if m.name == member_name else m for m in items]
                msg = f"已把「{raw}」設成「{member_name}」的其他寫法"
            table.save(items)
        return _redirect("/members", msg)

    # ------------------------------------------------------------------ 小團（config/teams.csv）

    @ui.post("/teams/save")
    def teams_save(name: str = Form(""), aliases: str = Form(""), members: str = Form(""),
                   active: str = Form(""), note: str = Form(""), original_name: str = Form("")):
        name = name.strip()
        if not name:
            return _redirect("/members/teams", "請填小團名稱", "error")
        team = Team(name=name, aliases=parse_list(aliases), members=parse_list(members),
                    active=active == "on", note=note.strip())
        directory = Directory(MemberTable(paths.members_file).load().items)
        back = f"/members/teams?edit_team={quote(original_name)}" if original_name else "/members/teams?new=1"
        if strangers := [m for m in team.members if directory.lookup(m) is None]:  # 成員只能是同工名單上的人
            return _redirect(back, f"「{'、'.join(strangers)}」不在同工名單上，先到「名單」新增再加進小團", "error")
        for key in (team.name, *team.aliases):  # 團名撞到人名 → 服事表寫這個只會對到那個人
            if (owner := directory.lookup(key)) is not None:
                return _redirect("/members/teams", f"「{key}」已經是同工「{owner.name}」的名字或其他寫法，"
                                                   "服事表寫這個只會對到那個人，請換一個寫法", "error")
        with TABLE_WRITE_LOCK:
            table = TeamTable(paths.teams_file)
            table.save(upsert(table.load().items, team, key=lambda t: t.name, original_key=original_name or None))
        return _redirect("/members/teams", f"已儲存小團「{name}」")

    @ui.post("/teams/delete")
    def teams_delete(name: str = Form(...)):
        with TABLE_WRITE_LOCK:
            table = TeamTable(paths.teams_file)
            table.save(remove(table.load().items, name, key=lambda t: t.name))
        return _redirect("/members/teams", f"已刪除小團「{name}」")

    # ------------------------------------------------------------------ settings

    def settings_page_response(request: Request, values: dict[str, Any] | None = None, error: str = "",
                               broken: str = "") -> HTMLResponse:
        env = {**read_env_file(paths.env_file), **{k: v for k, v in os.environ.items() if k.startswith(("LINE_", "UI_"))}}
        if values is None:
            values = form_values(Settings()) if broken else form_values(load_settings(paths))
        return page(request, "settings.html", f=values, error=error, broken=broken,
                    default_template=DEFAULT_TEMPLATE, token_status=_mask(env.get("LINE_CHANNEL_ACCESS_TOKEN", "")),
                    secret_status=_mask(env.get("LINE_CHANNEL_SECRET", "")),
                    password_status="已設定" if env.get("UI_PASSWORD") else "")

    @ui.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        try:
            return settings_page_response(request)
        except ConfigError as exc:
            return settings_page_response(request, broken=f"{exc.message}（{exc.hint}）")

    @ui.post("/settings", response_class=HTMLResponse)
    async def settings_save(request: Request):
        form = {k: str(v) for k, v in (await request.form()).items()}
        with SETTINGS_LOCK:
            try:
                current = load_settings(paths)
            except ConfigError:
                current = Settings()  # 設定檔壞了：用預設值為底，存檔後就修好了
            try:
                new = apply_form(current, form)
            except ChurchBotError as exc:
                return settings_page_response(request, values={**form_values(current), **form},
                                              error=f"{exc.message}　{exc.hint}".strip())
            save_settings(paths, new)
        await run_in_threadpool(scheduler.reload)
        return _redirect("/settings", f"設定已儲存。自動發送：{scheduler.status}")

    @ui.post("/remote-config/approve")
    def remote_config_approve():
        pending = webhook.verifier.take("管理網頁核准")
        if pending is None:
            return _redirect("/", "這個修改已經過期或被處理掉了", "warn")
        webhook.apply(pending)
        return _redirect("/", f"已核准：{pending.option.key} → {pending.value_text}（LINE 那邊不會另外通知）")

    @ui.post("/remote-config/reject")
    def remote_config_reject():
        pending = webhook.verifier.take("管理網頁拒絕")
        if pending is not None:
            log.warning("管理網頁拒絕了 LINE 設定修改：%s → %s（%s）", pending.option.key, pending.value_text,
                        pending.requester or pending.user_id)
        return _redirect("/", "已拒絕這個修改" if pending else "這個修改已經過期或被處理掉了")

    @ui.post("/settings/secrets")
    def settings_secrets(token: str = Form(""), secret: str = Form(""), password: str = Form(""),
                         clear_password: str = Form("")):
        updates = {k: v.strip() for k, v in (("LINE_CHANNEL_ACCESS_TOKEN", token), ("LINE_CHANNEL_SECRET", secret),
                                            ("UI_PASSWORD", password)) if v.strip()}
        if clear_password == "on":
            updates["UI_PASSWORD"] = ""
        if not updates:
            return _redirect("/settings", "沒有輸入任何新的金鑰，所以沒有變更", "warn")
        update_env_file(paths.env_file, updates)
        return _redirect("/settings", "LINE 金鑰 / 密碼已更新（存在 .env，不會上傳到 git）")

    # ------------------------------------------------------------------ 🧪 lab: 大教會架構（見 org.py）

    def org_report() -> tuple[OrgReport, list[OrgUnit], list[Issue], list[Target], list[Member]]:
        ctx = service.load()
        loaded = OrgTable(paths.org_file).load()
        sizes = {chat_id: count for chat_id, (count, _) in service.history.member_counts().items()}
        report = build_org(loaded.items, ctx.targets, ctx.members, sizes, ctx.settings.schedule.every_n_weeks)
        return report, loaded.items, [*loaded.issues, *report.issues], ctx.targets, ctx.members

    @ui.get("/lab/org", response_class=HTMLResponse)
    def lab_org(request: Request, edit: str = "", parent: str = ""):
        report, units, issues, targets, members = org_report()
        editing = next((u for u in units if u.name == edit), None)
        blocked = ({editing.name} | report.descendants(editing.name)) if editing else set()
        return page(request, "lab_org.html", report=report, units=units, issues=issues, targets=targets,
                    members=members, editing=editing, new_parent=parent, group_owners=report.group_owners(),
                    parent_choices=[u.name for u in units if u.name not in blocked], kinds=KIND_SUGGESTIONS)

    @ui.post("/lab/org/save")
    def lab_org_save(name: str = Form(""), parent: str = Form(""), kind: str = Form(""), leader: str = Form(""),
                     groups: list[str] = Form([]), members: str = Form(""), note: str = Form(""),
                     original_name: str = Form("")):
        name, parent = name.strip(), parent.strip()
        back = f"/lab/org?edit={quote(original_name)}#edit" if original_name else "/lab/org#edit"
        if not name:
            return _redirect(back, "請填單位名稱", "error")
        with TABLE_WRITE_LOCK:
            table = OrgTable(paths.org_file)
            units = table.load().items
            if name != original_name and any(u.name == name for u in units):
                return _redirect(back, f"已經有叫「{name}」的單位了", "error")
            if original_name and parent:
                report, *_ = org_report()
                if parent == original_name or parent in report.descendants(original_name):
                    return _redirect(back, "上層單位不能選自己或自己底下的單位", "error")
            unit = OrgUnit(name=name, parent=parent, kind=kind.strip(), leader=leader.strip(),
                           groups=tuple(g for g in groups if g), members=parse_list(members), note=note.strip())
            units = upsert(units, unit, key=lambda u: u.name, original_key=original_name or None)
            if original_name and original_name != name:  # 改名：下層單位的「上層」跟著改
                units = [replace(u, parent=name) if u.parent == original_name else u for u in units]
            table.save(units)
        return _redirect("/lab/org", f"已儲存「{name}」")

    @ui.post("/lab/org/delete")
    def lab_org_delete(name: str = Form(...)):
        with TABLE_WRITE_LOCK:
            table = OrgTable(paths.org_file)
            units = table.load().items
            gone = next((u for u in units if u.name == name), None)
            if gone is None:
                return _redirect("/lab/org", f"找不到「{name}」", "warn")
            # 下層單位往上接到被刪單位的上層，不會跟著消失
            table.save([replace(u, parent=gone.parent) if u.parent == name else u for u in units if u is not gone])
        return _redirect("/lab/org", f"已刪除「{name}」（它底下的單位移到上一層）")

    @ui.post("/lab/org/refresh-sizes")
    def lab_org_refresh_sizes():
        settings = load_settings(paths)
        if settings.messenger.kind != "line":
            return _redirect("/lab/org", "目前是測試模式，沒有連 LINE，查不到群組人數", "warn")
        groups = [t for t in TargetTable(paths.targets_file).load().items if t.line_id[:1] in ("C", "R")
                  and LINE_ID_RE.match(t.line_id)]
        messenger = build_messenger(settings, paths)
        updated, failed = 0, []
        try:
            for target in groups:
                try:
                    size = messenger.audience_size(target.line_id)
                except ChurchBotError:
                    failed.append(target.name)
                    continue
                if size is not None:
                    service.history.set_member_count(target.line_id, size)
                    updated += 1
        finally:
            messenger.close()
        if failed:
            return _redirect("/lab/org", f"更新了 {updated} 個群組的人數；查不到：{'、'.join(failed)}（機器人可能不在群組裡了）",
                             "warn")
        return _redirect("/lab/org", f"更新了 {updated} 個群組的人數")

    # ------------------------------------------------------------------ history & help

    @ui.get("/runs", response_class=HTMLResponse)
    def runs_page(request: Request):
        return page(request, "runs.html", runs=service.history.recent_runs(50))

    @ui.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_detail(request: Request, run_id: str):
        report = service.history.get_report(run_id)
        if report is None:
            raise HTTPException(404, "找不到這筆紀錄")
        return page(request, "run_detail.html", r=report)

    @ui.get("/help", response_class=HTMLResponse)
    def help_page(request: Request):
        return page(request, "help.html")

    @ui.get("/download/{name}")
    def download(name: str):
        files = {"targets.csv": paths.targets_file, "members.csv": paths.members_file,
                 "teams.csv": paths.teams_file}
        if name not in files or not files[name].exists():
            raise HTTPException(404, "找不到檔案")
        return FileResponse(files[name], filename=name, media_type="text/csv")

    # ------------------------------------------------------------------ JSON API

    def report_json(report: RunReport) -> dict[str, Any]:
        return {**jsonable_encoder(report), "status": report.status}

    @api.get("/status", summary="目前狀態")
    def api_status():
        last = service.history.last_run()
        return {"version": __version__, "schedule": scheduler.status,
                "next_run": scheduler.next_run().isoformat() if scheduler.next_run() else None,
                "last_run": jsonable_encoder(last) if last else None}

    @api.get("/preview", summary="預覽這次會發的訊息（不會送出）")
    def api_preview():
        report, _ = service.preview()
        return report_json(report)

    @api.post("/send", summary="立刻發送一次")
    def api_send(force: bool = False):
        report, _ = service.run("manual", force=force)
        return report_json(report)

    @api.get("/check", summary="健康檢查")
    def api_check():
        return jsonable_encoder(service.health())

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

    app.include_router(ui)
    app.include_router(api)
    return app
