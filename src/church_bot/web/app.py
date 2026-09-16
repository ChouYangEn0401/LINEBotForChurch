"""管理網頁（FastAPI + Jinja2，伺服器端產生畫面，不需要任何前端框架或建置步驟）。

頁面：首頁（狀態 + 本週預覽 + 發送）、群組、人員、設定、紀錄、系統檢查、使用說明。
JSON API：/api/*（自動產生的文件在 /docs）。LINE Webhook：/line/webhook（選用）。
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from church_bot import __version__
from church_bot.config import (
    DEFAULT_TEMPLATE, WEEKDAY_ZH, Paths, Settings, load_settings, read_env_file, save_settings, update_env_file,
)
from church_bot.core.renderer import Renderer
from church_bot.errors import ChurchBotError, ConfigError
from church_bot.messengers import MESSENGER_KINDS_ZH, build_messenger
from church_bot.models import DeliveryStatus, Member, OutgoingMessage, RunReport, Severity, Target
from church_bot.scheduler import BotScheduler
from church_bot.service import BotService
from church_bot.sources import SOURCE_KINDS_ZH
from church_bot.sources.google_public import parse_sheet_url
from church_bot.tables import (
    LINE_ID_RE, TABLE_WRITE_LOCK, MemberTable, TargetTable, describe_line_id, fmt_list, parse_list, remove, upsert,
)
from church_bot.webhook import SignatureError, WebhookHandler

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
LAYOUTS_ZH = {"auto": "自動判斷（推薦）", "wide": "日期在左、一列一次聚會", "long": "一列一項服事",
              "matrix": "日期在上、一欄一次聚會"}
SEVERITY_ICON = {"error": "❌", "warning": "⚠️", "info": "ℹ️"}


def _redirect(url: str, msg: str = "", level: str = "ok") -> RedirectResponse:
    if msg:
        url = f"{url}{'&' if '?' in url else '?'}msg={quote(msg)}&level={level}"
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
    try:
        new = Settings.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        raise ConfigError(f"「{'.'.join(str(x) for x in first['loc'])}」{first['msg']}") from exc

    admin = new.line.admin_target_id
    if admin and not LINE_ID_RE.match(admin):
        raise ConfigError("管理員 LINE ID 格式不對", "要是 U 或 C 開頭再加 32 個英數字。私訊機器人「我的ID」就能拿到。")
    if new.source.kind in ("google_public", "google_service_account"):
        parse_sheet_url(new.source.spreadsheet_url)
    Renderer(new.message).validate()
    return new


def create_app(paths: Paths) -> FastAPI:
    service = BotService(paths)
    scheduler = BotScheduler(service)
    webhook = WebhookHandler(service)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.globals.update(
        version=__version__, weekday_zh=WEEKDAY_ZH, describe_line_id=describe_line_id, fmt_list=fmt_list,
        severity_icon=SEVERITY_ICON, status_zh={s.value: s.zh for s in DeliveryStatus},
        source_kinds=SOURCE_KINDS_ZH, messenger_kinds=MESSENGER_KINDS_ZH, layouts=LAYOUTS_ZH,
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
    basic = HTTPBasic(auto_error=False)

    def require_login(credentials: HTTPBasicCredentials | None = Depends(basic)) -> None:
        password = os.environ.get("UI_PASSWORD") or read_env_file(paths.env_file).get("UI_PASSWORD", "")
        if not password:
            return
        if credentials and secrets.compare_digest(credentials.password.encode(), password.encode()):
            return
        raise HTTPException(401, "需要密碼（帳號隨便填，密碼是 .env 裡的 UI_PASSWORD）",
                            headers={"WWW-Authenticate": 'Basic realm="church-bot"'})

    ui = APIRouter(dependencies=[Depends(require_login)])
    api = APIRouter(prefix="/api", tags=["API"], dependencies=[Depends(require_login)])

    def page(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        ctx.setdefault("flash", request.query_params.get("msg", ""))
        ctx.setdefault("flash_level", request.query_params.get("level", "ok"))
        base = {"nav": name.removesuffix(".html"), "schedule_status": scheduler.status,
                "next_run": scheduler.next_run_text()}
        return templates.TemplateResponse(request, name, {**base, **ctx})

    @app.exception_handler(ChurchBotError)
    async def friendly_error(request: Request, exc: ChurchBotError) -> HTMLResponse:
        return page(request, "error.html", message=exc.message, hint=exc.hint)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> HTMLResponse:
        log.exception("網頁發生未預期的錯誤")
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
        )

    @ui.post("/send")
    def send(force: int = Form(0)):
        report, _ = service.run("manual", force=bool(force))
        msg, level = run_summary(report)
        return _redirect(f"/runs/{report.run_id}", msg, level)

    @ui.get("/check", response_class=HTMLResponse)
    def check(request: Request):
        return page(request, "check.html", items=service.health())

    # ------------------------------------------------------------------ targets

    @ui.get("/targets", response_class=HTMLResponse)
    def targets_page(request: Request, edit: str = ""):
        result = TargetTable(paths.targets_file).load()
        known = {t.line_id for t in result.items}
        chats = [c for c in service.history.chats()
                 if c["chat_id"] not in known and c["status"] == "active" and c["kind"] in ("group", "room")]
        editing = next((t for t in result.items if t.name == edit), None)
        return page(request, "targets.html", targets=result.items, issues=result.issues, chats=chats, editing=editing)

    @ui.post("/targets/save")
    def targets_save(name: str = Form(""), line_id: str = Form(""), enabled: str = Form(""), roles: str = Form(""),
                     labels: str = Form(""), mention: str = Form(""), note: str = Form(""),
                     original_name: str = Form("")):
        name, line_id = name.strip(), line_id.strip()
        if not name:
            return _redirect("/targets", "請填群組名稱", "error")
        if line_id and not LINE_ID_RE.match(line_id):
            return _redirect(f"/targets?edit={quote(original_name)}",
                             f"LINE_ID 格式不對：「{line_id}」。要是 C 或 U 開頭再加 32 個英數字", "error")
        target = Target(name=name, line_id=line_id, enabled=enabled == "on", roles=parse_list(roles),
                        labels=parse_list(labels), mention=mention == "on", note=note.strip())
        with TABLE_WRITE_LOCK:
            table = TargetTable(paths.targets_file)
            table.save(upsert(table.load().items, target, key=lambda t: t.name, original_key=original_name or None))
        if target.enabled and not line_id:
            return _redirect("/targets", f"已儲存「{name}」，但還沒填 LINE_ID，所以不會收到提醒", "warn")
        return _redirect("/targets", f"已儲存「{name}」")

    @ui.post("/targets/delete")
    def targets_delete(name: str = Form(...)):
        with TABLE_WRITE_LOCK:
            table = TargetTable(paths.targets_file)
            table.save(remove(table.load().items, name, key=lambda t: t.name))
        return _redirect("/targets", f"已刪除「{name}」")

    @ui.post("/targets/add-chat")
    def targets_add_chat(chat_id: str = Form(...), name: str = Form("")):
        with TABLE_WRITE_LOCK:
            table = TargetTable(paths.targets_file)
            items = table.load().items
            if not any(t.line_id == chat_id for t in items):
                items.append(Target(name=name or f"新群組 {dt.date.today():%m/%d}", line_id=chat_id, enabled=False))
                table.save(items)
        return _redirect(f"/targets?edit={quote(name or '')}", "已加入群組表（尚未啟用），確認後勾選「啟用」並儲存")

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
    def members_page(request: Request, edit: str = ""):
        result = MemberTable(paths.members_file).load()
        try:
            unknown, unknown_error = service.unknown_names(), ""
        except ChurchBotError as exc:
            unknown, unknown_error = {}, f"{exc.message}（{exc.hint}）" if exc.hint else exc.message
        known_uids = {m.line_user_id for m in result.items if m.line_user_id}
        people = [p for p in service.history.people() if p["user_id"] not in known_uids]
        editing = next((m for m in result.items if m.name == edit), None)
        return page(request, "members.html", members=result.items, issues=result.issues, unknown=unknown,
                    unknown_error=unknown_error, people=people, editing=editing)

    @ui.post("/members/save")
    def members_save(name: str = Form(""), aliases: str = Form(""), line_user_id: str = Form(""),
                     active: str = Form(""), note: str = Form(""), original_name: str = Form("")):
        name, uid = name.strip(), line_user_id.strip()
        if not name:
            return _redirect("/members", "請填名字", "error")
        if uid and not (LINE_ID_RE.match(uid) and uid.startswith("U")):
            return _redirect(f"/members?edit={quote(original_name)}",
                             f"LINE_userId 格式不對：「{uid}」。要是 U 開頭再加 32 個英數字", "error")
        member = Member(name=name, aliases=parse_list(aliases), line_user_id=uid, active=active == "on", note=note.strip())
        with TABLE_WRITE_LOCK:
            table = MemberTable(paths.members_file)
            table.save(upsert(table.load().items, member, key=lambda m: m.name, original_key=original_name or None))
        return _redirect("/members", f"已儲存「{name}」")

    @ui.post("/members/delete")
    def members_delete(name: str = Form(...)):
        with TABLE_WRITE_LOCK:
            table = MemberTable(paths.members_file)
            table.save(remove(table.load().items, name, key=lambda m: m.name))
        return _redirect("/members", f"已刪除「{name}」")

    @ui.post("/members/add-person")
    def members_add_person(user_id: str = Form(...), name: str = Form("")):
        with TABLE_WRITE_LOCK:
            table = MemberTable(paths.members_file)
            items = table.load().items
            if not any(m.line_user_id == user_id for m in items):
                display = name or f"新朋友 {user_id[-6:]}"
                items.append(Member(name=display, line_user_id=user_id, active=False,
                                    note=f"機器人自動收集（{dt.date.today():%m/%d}），確認後把「狀態」改成「服事中」"))
                table.save(items)
                return _redirect(f"/members?edit={quote(display)}", f"已加入人員表「{display}」（尚未啟用）")
        return _redirect("/members", "這個人已經在人員表裡了")

    @ui.post("/members/forget-person")
    def members_forget_person(user_id: str = Form(...)):
        service.history.forget_person(user_id)
        return _redirect("/members", "已從「自動收集到的人」移除")

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
                items = [Member(m.name, (*m.aliases, raw) if raw not in m.aliases else m.aliases, m.line_user_id,
                                m.active, m.note) if m.name == member_name else m for m in items]
                msg = f"已把「{raw}」設成「{member_name}」的其他寫法"
            table.save(items)
        return _redirect("/members", msg)

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
        files = {"targets.csv": paths.targets_file, "members.csv": paths.members_file}
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
        try:
            count = await run_in_threadpool(webhook.handle, body, request.headers.get("x-line-signature", ""))
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
