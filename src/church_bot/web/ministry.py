"""一個牧區自己的後台：/m/<編號>/…（主控台、服事表、同工名單、LINE 群組、發送紀錄、設定、系統檢查）。

每一頁跟單一牧區時一模一樣，只是多了「哪個牧區」（``m``，見 common.MinistryView）：
讀寫的是那個牧區資料夾裡的設定和名單、那個牧區的資料庫，網址前面多了 /m/<編號>。
進來之前已經檢查過身分（網站密碼 common.Web.require_login；牧區密碼或伺服器管理員 Web.ministry）。
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import replace
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import ValidationError

from church_bot import __version__
from church_bot.church import MIN_MINISTRY_PASSWORD, edit_ministry, hash_password, remove_ministry
from church_bot.config import DEFAULT_TEMPLATE, SETTINGS_LOCK, Settings, load_settings, save_settings, update_settings
from church_bot.core.accounts import build_accounts, duplicate_names, exact_links
from church_bot.core.dates import format_date
from church_bot.core.directory import Directory, normalize_name, validate_teams
from church_bot.core.planner import DATE_FMT
from church_bot.core.renderer import Renderer
from church_bot.errors import ChurchBotError, ConfigError
from church_bot.messengers import build_messenger
from church_bot.models import Member, OutgoingMessage, RunReport, Severity, Target, Team
from church_bot.sources.google_public import parse_sheet_url
from church_bot.tables import (
    LINE_ID_RE, TABLE_WRITE_LOCK, MemberTable, TargetTable, TeamTable, fmt_list, parse_list, remove, upsert,
)
from church_bot.web.common import (
    MinistryView, Web, ministry_cookie, ministry_token, quota_json, redirect, run_summary, safe_next,
)
from church_bot.web.overview import Step, build_steps

log = logging.getLogger(__name__)


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


def ministry_routes(web: Web) -> tuple[APIRouter, APIRouter]:
    """回傳 (畫面, JSON API) 兩個 router，網址都在 /m/<編號> 底下。"""
    ui = APIRouter(prefix="/m/{mid}", dependencies=[Depends(web.require_login)])
    api = APIRouter(prefix="/m/{mid}/api", tags=["牧區 API"], dependencies=[Depends(web.require_login)])

    # ------------------------------------------------------------------ dashboard

    @ui.get("/", response_class=HTMLResponse)
    def index(request: Request, m: MinistryView = Depends(web.ministry)):
        report, plan = m.service.preview()
        return web.page(
            request, "index.html", m, report=report, plan=plan, last=m.service.history.last_run(),
            problems=[i for i in report.issues if i.severity is not Severity.INFO],
            infos=[i for i in report.issues if i.severity is Severity.INFO],
            steps=workflow_steps(m, report), quota=quota_json(m.service.quota_status()),
        )

    def workflow_steps(m: MinistryView, report: RunReport) -> list[Step]:
        try:
            ctx = m.service.load()
        except ChurchBotError:
            return []  # 設定檔壞了：上面的「需要處理的事項」已經會說明
        roster, roster_error = None, ""
        try:
            roster = m.service.fetch_roster(ctx.settings, m.service.now(ctx.settings).date(), use_cache=True)
        except ChurchBotError as exc:
            roster_error = exc.message
        accounts = build_accounts(m.service.history.people(), ctx.members)
        unknown = sum(1 for i in report.issues if i.code == "unknown_name")
        return build_steps(issues=report.issues, settings=ctx.settings, roster=roster, roster_error=roster_error,
                           members=ctx.members, targets=ctx.targets, unknown_names=unknown,
                           pending_claims=sum(1 for a in accounts if a.needs_review and not a.ignored),
                           next_run=web.scheduler.next_run_text(m.id))

    @api.get("/nav", summary="側欄每一項現在的狀況（那行小字和右邊的數字）")
    def api_nav(m: MinistryView = Depends(web.ministry)):
        """側欄不只是選單：每一項底下寫的是「它現在怎麼樣」，有事要處理的會標數字。

        頁面先畫出來、再由 app.js 來問這一支，所以讀 Google Sheet 慢的時候不會卡住整頁。
        """
        tone = {"ok": "ok", "warning": "warn", "error": "bad", "off": ""}
        report, _plan = m.service.preview()
        problems = [i for i in report.issues if i.severity is not Severity.INFO]
        errors = [i for i in problems if i.is_error]
        status: dict[str, dict[str, Any]] = {"index": {
            "sub": f"{len(errors)} 個問題會擋住發送" if errors else
                   f"{len(problems)} 件事要看一下" if problems else "一切正常",
            "count": len(problems), "tone": "bad" if errors else "warn" if problems else "ok",
        }}
        for step in workflow_steps(m, report):
            key = {"服事表": "roster", "同工名單": "members", "LINE 群組": "targets"}.get(step.title)
            if key:
                status[key] = {"sub": step.summary, "count": 0, "tone": tone[step.status]}
        if "members" in status:
            try:
                unknown = len(m.service.unknown_names())
            except ChurchBotError:
                unknown = 0
            members = MemberTable(m.paths.members_file).load().items
            pending = sum(1 for a in build_accounts(m.service.history.people(), members)
                          if a.needs_review and not a.ignored)
            parts = [f"{len(members)} 位" if members else "還沒設定"]
            parts += [f"{unknown} 個名字對不到"] if unknown else []
            parts += [f"{pending} 位待確認"] if pending else []
            status["members"].update(sub=" · ".join(parts), count=unknown + pending)
        last = m.service.history.last_run()
        status["runs"] = ({"sub": f"上次 {last.started_at[5:16].replace('-', '/').replace('T', ' ')} · {last.status_zh}",
                           "count": 0, "tone": tone.get(last.status, "")} if last
                          else {"sub": "還沒有發送過", "count": 0, "tone": ""})
        status["settings"] = {"sub": web.scheduler.status_for(m.id) if web.scheduler.next_run(m.id) else "自動發送關著",
                              "count": 0, "tone": ""}
        return status

    @ui.post("/send")
    def send(force: int = Form(0), m: MinistryView = Depends(web.ministry)):
        report, _ = m.service.run("manual", force=bool(force))
        msg, level = run_summary(report)
        return m.redirect(f"/runs/{report.run_id}", msg, level)

    @ui.get("/check", response_class=HTMLResponse)
    def check(request: Request, m: MinistryView = Depends(web.ministry)):
        return web.page(request, "check.html", m, items=m.service.health())

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
    def roster_page(request: Request, m: MinistryView = Depends(web.ministry)):
        ctx = None
        roster, roster_error, roster_hint = None, "", ""
        roles: list[str] = []
        parsed: list[dict] = []
        today = dt.date.today()
        window_end = today
        try:
            ctx = m.service.load()
            today = m.service.now(ctx.settings).date()
            window_end = today + dt.timedelta(days=ctx.settings.behavior.lookahead_days - 1)
            roster = m.service.fetch_roster(ctx.settings, today, use_cache=True)
            roles, parsed = _parsed_rows(roster, ctx.directory, today, window_end)
        except ChurchBotError as exc:
            roster_error, roster_hint = exc.message, exc.hint
        source = ctx.settings.source if ctx else None
        sheet_url = source.spreadsheet_url if source and source.kind.startswith("google") else ""
        return web.page(
            request, "roster.html", m, roster=roster, roster_error=roster_error, roster_hint=roster_hint,
            source=source, sheet_url=sheet_url, roles=roles, parsed=parsed, today=today, window_end=window_end,
            issues=[i for i in (roster.issues if roster else ()) if i.severity is not Severity.INFO],
            infos=[i for i in (roster.issues if roster else ()) if i.severity is Severity.INFO],
            last_date=format_date(roster.last_date, "%Y/%-m/%-d") if roster and roster.last_date else "",
        )

    @ui.post("/roster/refresh")
    def roster_refresh(m: MinistryView = Depends(web.ministry)):
        m.service.clear_roster_cache()
        return m.redirect("/roster", "已重新讀取服事表")

    @ui.post("/roster/source")
    def roster_source(url: str = Form(""), m: MinistryView = Depends(web.ministry)):
        """「服事表」頁最簡單的接法：貼 Google Sheet 網址 → 先試讀一次 → 讀得到才存。"""
        url = url.strip()
        if not url:
            return m.redirect("/roster", "請先貼上 Google Sheet 的網址", "error")
        try:
            parse_sheet_url(url)
        except ChurchBotError as exc:
            return m.redirect("/roster", f"{exc.message}。{exc.hint}", "error")
        with SETTINGS_LOCK:
            trial = load_settings(m.paths).model_copy(deep=True)
            trial.source.kind, trial.source.spreadsheet_url, trial.source.worksheet = "google_public", url, ""
            try:
                roster = m.service.fetch_roster(trial, m.service.now(trial).date())
            except ChurchBotError as exc:
                return m.redirect("/roster", f"還沒換過去，因為讀不到這份表：{exc.message}。{exc.hint}", "error")
            save_settings(m.paths, trial)
        last = format_date(roster.last_date, "%Y/%-m/%-d") if roster.last_date else "（沒有日期）"
        return m.redirect("/roster", f"已接上：{roster.source}，共 {len(roster.days)} 次聚會，排到 {last}")

    @api.get("/roster/sheet", summary="服事表的原始格子（給「服事表」頁的表格檢視用）")
    def api_roster_sheet(m: MinistryView = Depends(web.ministry)):
        try:
            ctx, sheets, infos, today = m.service.sheet_preview()
        except ChurchBotError as exc:
            return {"ok": False, "error": exc.message, "hint": exc.hint}
        try:
            unknown = list(m.service.unknown_names())
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

    def targets_response(request: Request, m: MinistryView, editing: Target | None = None, new: bool = False,
                         original_name: str | None = None, error: str = "") -> HTMLResponse:
        result = TargetTable(m.paths.targets_file).load()
        known = {t.line_id for t in result.items}
        # 機器人在這個牧區的群組裡看過、但清單裡沒有的；加上還沒分到任何牧區的（點「加入」就分到這個牧區）
        chats = [c for c in m.service.history.chats()
                 if c["chat_id"] not in known and c["status"] == "active" and c["kind"] in ("group", "room")]
        seen = {c["chat_id"] for c in chats}
        chats += [c for c in web.church.unassigned_chats() if c["chat_id"] not in known | seen]
        show_form = bool(editing) or new  # 清單和表單分開兩個畫面：一次只看一件事
        try:
            message = load_settings(m.paths).message
        except ConfigError:
            message = Settings().message
        extra = {"flash": error, "flash_level": "error"} if error else {}
        return web.page(request, "targets.html", m, targets=result.items, issues=result.issues, chats=chats, editing=editing,
                    show_form=show_form, roles_in_sheet=m.service.roster_roles() if show_form else [],
                    original_name=editing.name if original_name is None and editing else (original_name or ""),
                    message_defaults=message, **extra)

    @ui.get("/targets", response_class=HTMLResponse)
    def targets_page(request: Request, edit: str = "", new: str = "", m: MinistryView = Depends(web.ministry)):
        editing = next((t for t in TargetTable(m.paths.targets_file).load().items if t.name == edit), None)
        return targets_response(request, m, editing, new == "1")

    @ui.post("/targets/save")
    def targets_save(request: Request, name: str = Form(""), line_id: str = Form(""), enabled: str = Form(""),
                     roles: str = Form(""), labels: str = Form(""), mention: str = Form(""), note: str = Form(""),
                     title: str = Form(""), footer: str = Form(""), template: str = Form(""),
                     original_name: str = Form(""), m: MinistryView = Depends(web.ministry)):
        name, line_id = name.strip(), line_id.strip()
        target = Target(name=name, line_id=line_id, enabled=enabled == "on", roles=parse_list(roles),
                        labels=parse_list(labels), mention=mention == "on", note=note.strip(), title=title.strip(),
                        footer=footer.strip(), template=template.replace("\r\n", "\n").strip())
        # 錯誤時直接把表單畫回去（不跳轉），剛打好的訊息模板才不會不見
        if not name:
            return targets_response(request, m, target, original_name=original_name, error="請填群組名稱")
        if line_id and not LINE_ID_RE.match(line_id):
            return targets_response(request, m, target, original_name=original_name,
                                    error=f"LINE_ID 格式不對：「{line_id}」。要是 C 或 U 開頭再加 32 個英數字")
        try:
            Renderer(load_settings(m.paths).message).validate(target)
        except ChurchBotError as exc:
            return targets_response(request, m, target, original_name=original_name,
                                    error=f"還沒儲存：{exc.message}。{exc.hint}")
        with TABLE_WRITE_LOCK:
            table = TargetTable(m.paths.targets_file)
            table.save(upsert(table.load().items, target, key=lambda t: t.name, original_key=original_name or None))
        if target.enabled and not line_id:
            return m.redirect("/targets", f"已儲存「{name}」，但還沒填 LINE_ID，所以不會收到提醒", "warn")
        if target.has_own_message:  # 改了訊息：留在編輯畫面，下面就是這個群組現在會收到的樣子
            return m.redirect(f"/targets?edit={quote(name)}#preview", f"已儲存「{name}」，下面是這個群組現在會收到的訊息")
        return m.redirect("/targets", f"已儲存「{name}」")

    @ui.post("/targets/delete")
    def targets_delete(name: str = Form(...), m: MinistryView = Depends(web.ministry)):
        with TABLE_WRITE_LOCK:
            table = TargetTable(m.paths.targets_file)
            table.save(remove(table.load().items, name, key=lambda t: t.name))
        return m.redirect("/targets", f"已刪除「{name}」")

    @ui.post("/targets/add-chat")
    def targets_add_chat(chat_id: str = Form(...), name: str = Form(""), m: MinistryView = Depends(web.ministry)):
        if any(c["chat_id"] == chat_id for c in web.church.unassigned_chats()):  # 還沒分到牧區的：分到這個牧區
            target = web.church.assign_chat(chat_id, m.id, name)
            return m.redirect(f"/targets?edit={quote(target.name)}",
                              f"已把群組分到「{m.name}」（尚未啟用），確認後勾選「要收到提醒」並儲存")
        name = name or f"新群組 {dt.date.today():%m/%d}"
        with TABLE_WRITE_LOCK:
            table = TargetTable(m.paths.targets_file)
            items = table.load().items
            if existing := next((t for t in items if t.line_id == chat_id), None):
                name = existing.name
            else:
                items.append(Target(name=name, line_id=chat_id, enabled=False))
                table.save(items)
        return m.redirect(f"/targets?edit={quote(name)}", "已加入 LINE 群組（尚未啟用），確認後勾選「啟用」並儲存")

    @ui.post("/targets/test")
    def targets_test(name: str = Form(...), m: MinistryView = Depends(web.ministry)):
        target = next((t for t in TargetTable(m.paths.targets_file).load().items if t.name == name), None)
        if target is None or not LINE_ID_RE.match(target.line_id):
            return m.redirect("/targets", f"「{name}」沒有正確的 LINE_ID，無法測試", "error")
        messenger = build_messenger(load_settings(m.paths), m.paths)
        try:
            messenger.send(target.line_id, OutgoingMessage(
                text=f"✅ 測試訊息：服事提醒機器人已經可以送訊息到「{target.name}」了！\n之後會在這裡自動提醒每週的服事 🙏"))
        finally:
            messenger.close()
        return m.redirect("/targets", f"已送出測試訊息到「{name}」，請到 LINE 確認有沒有收到")

    # ------------------------------------------------------------------ members

    @ui.get("/members", response_class=HTMLResponse)
    def members_page(request: Request, edit: str = "", new: str = "", m: MinistryView = Depends(web.ministry)):
        """名單本身。LINE 帳號、小團各自有子頁，這一頁只做「誰在服事、名字怎麼寫」。"""
        result = MemberTable(m.paths.members_file).load()
        try:
            unknown, unknown_error = m.service.unknown_names(), ""
        except ChurchBotError as exc:
            unknown, unknown_error = {}, f"{exc.message}（{exc.hint}）" if exc.hint else exc.message
        accounts = build_accounts(m.service.history.people(), result.items)
        editing = next((x for x in result.items if x.name == edit), None)
        return web.page(request, "members.html", m, members=result.items, issues=result.issues, unknown=unknown,
                    unknown_error=unknown_error, editing=editing, show_form=bool(editing) or new == "1",
                    line_names={a.user_id: a.display_name for a in accounts},
                    pending_accounts=sum(1 for a in accounts if a.needs_review and not a.ignored))

    @ui.get("/members/accounts", response_class=HTMLResponse)
    def members_accounts_page(request: Request, m: MinistryView = Depends(web.ministry)):
        """LINE 帳號 ↔ 同工名單：誰登記了名字等確認、哪個帳號是名單上的誰。"""
        result = MemberTable(m.paths.members_file).load()
        try:
            collect_names = load_settings(m.paths).chat.collect_names
        except ConfigError:
            collect_names = False
        accounts = build_accounts(m.service.history.people(), result.items)
        return web.page(request, "members_accounts.html", m, members=result.items, collect_names=collect_names,
                    accounts=[a for a in accounts if not a.ignored],
                    ignored_accounts=[a for a in accounts if a.ignored], linkable=exact_links(accounts, result.items))

    @ui.get("/members/teams", response_class=HTMLResponse)
    def members_teams_page(request: Request, edit_team: str = "", new: str = "", m: MinistryView = Depends(web.ministry)):
        """小團：服事表寫團名，提醒就列出成員。"""
        result = MemberTable(m.paths.members_file).load()
        teams = TeamTable(m.paths.teams_file).load()
        editing_team = next((t for t in teams.items if t.name == edit_team), None)
        return web.page(request, "members_teams.html", m, members=result.items, teams=teams.items, editing_team=editing_team,
                    show_form=bool(editing_team) or new == "1",
                    team_issues=[*teams.issues, *validate_teams(teams.items, result.items)])

    @ui.post("/members/save")
    def members_save(name: str = Form(""), aliases: str = Form(""), line_user_id: str = Form(""),
                     active: str = Form(""), note: str = Form(""), original_name: str = Form(""), admin: str = Form(""), m: MinistryView = Depends(web.ministry)):
        name, uid = name.strip(), line_user_id.strip()
        if not name:
            return m.redirect("/members", "請填名字", "error")
        if uid and not (LINE_ID_RE.match(uid) and uid.startswith("U")):
            return m.redirect(f"/members?edit={quote(original_name)}",
                             f"LINE_userId 格式不對：「{uid}」。要是 U 開頭再加 32 個英數字", "error")
        member = Member(name=name, aliases=parse_list(aliases), line_user_id=uid, active=active == "on", note=note.strip(),
                        admin=admin == "on")
        with TABLE_WRITE_LOCK:
            table = MemberTable(m.paths.members_file)
            table.save(upsert(table.load().items, member, key=lambda x: x.name, original_key=original_name or None))
        return m.redirect("/members", f"已儲存「{name}」")

    @ui.post("/members/admin")
    def members_admin(name: str = Form(...), enabled: str = Form(""), next_url: str = Form("", alias="next"),
                      m: MinistryView = Depends(web.ministry)):
        """把某位同工勾成（或取消）管理員。能開管理網頁的人本來就能改所有設定，所以不用另外驗證。"""
        on = enabled == "1"
        next_url = safe_next(next_url) if next_url.startswith(m.base + "/") else m.url("/members")
        with TABLE_WRITE_LOCK:
            table = MemberTable(m.paths.members_file)
            items = table.load().items
            member = next((x for x in items if x.name == name), None)
            if member is None:
                return redirect(next_url, f"同工名單裡找不到「{name}」", "error")
            table.save([replace(x, admin=on) if x is member else x for x in items])
        if on and not member.line_user_id:
            return redirect(next_url, f"「{name}」已經是管理員，但還沒對應 LINE 帳號，所以在 LINE 上還認不出他", "warn")
        return redirect(next_url, f"「{name}」{'已經是管理員了' if on else '不再是管理員'}")

    @ui.post("/members/delete")
    def members_delete(name: str = Form(...), m: MinistryView = Depends(web.ministry)):
        with TABLE_WRITE_LOCK:
            table = MemberTable(m.paths.members_file)
            table.save(remove(table.load().items, name, key=lambda x: x.name))
        return m.redirect("/members", f"已刪除「{name}」")

    # --- 其他寫法（一個人可以有很多個稱呼，一次加一個 / 刪一個） ---

    def _change_aliases(m: MinistryView, name: str, change, fragment: str = ""):  # noqa: ANN001
        """把某位同工的「其他寫法」換成 change(他, 全部同工) 算出來的結果。

        ``change`` 回傳 (新的其他寫法, 不行的原因)；有原因就不存檔。
        這裡回傳「要給瀏覽器的錯誤畫面」或 None（成功）。
        """
        with TABLE_WRITE_LOCK:
            table = MemberTable(m.paths.members_file)
            items = table.load().items
            member = next((x for x in items if x.name == name), None)
            if member is None:
                return m.redirect(f"/members{fragment}", f"同工名單裡找不到「{name}」", "error")
            aliases, problem = change(member, items)
            if problem:
                return m.redirect(f"/members{fragment}", problem, "error")
            table.save([replace(x, aliases=aliases) if x is member else x for x in items])
        return None

    @ui.post("/members/aliases/add")
    def alias_add(name: str = Form(...), alias: str = Form(...), m: MinistryView = Depends(web.ministry)):
        alias = alias.strip()
        if not alias:
            return m.redirect("/members", "請填要新增的寫法", "error")

        def change(member: Member, items: list[Member]):
            if any(normalize_name(a) == normalize_name(alias) for a in (member.name, *member.aliases)):
                return member.aliases, f"「{alias}」已經是「{member.name}」的寫法了"
            others = [x for x in items if x is not member]
            if (owner := Directory(others).lookup(alias)) is not None:
                return member.aliases, f"「{alias}」已經是同工「{owner.name}」的名字或其他寫法，不能重複"
            return (*member.aliases, alias), ""

        if bad := _change_aliases(m, name, change):
            return bad
        return m.redirect("/members", f"已把「{alias}」加成「{name}」的其他寫法")

    @ui.post("/members/aliases/remove")
    def alias_remove(name: str = Form(...), alias: str = Form(...), m: MinistryView = Depends(web.ministry)):
        def change(member: Member, _items: list[Member]):
            return tuple(a for a in member.aliases if a != alias), ""

        if bad := _change_aliases(m, name, change):
            return bad
        return m.redirect("/members", f"已把「{alias}」從「{name}」的其他寫法移除")

    # --- LINE 帳號 ↔ 同工名單（按鈕說明見 core/accounts.py） ---

    @ui.post("/members/accounts/add")
    def account_add(user_id: str = Form(...), m: MinistryView = Depends(web.ministry)):
        person = m.service.history.person(user_id)
        if person is None:
            return m.redirect("/members/accounts", "找不到這個 LINE 帳號（可能已經刪掉了）", "error")
        claimed = person["real_name"]
        name = claimed or person["display_name"] or f"新朋友 {user_id[-6:]}"
        with TABLE_WRITE_LOCK:
            table = MemberTable(m.paths.members_file)
            items = table.load().items
            if any(x.line_user_id == user_id for x in items):
                return m.redirect("/members/accounts", "這個 LINE 帳號已經在同工名單裡了", "warn")
            if (same := Directory(items).lookup(name)) is not None:
                return m.redirect("/members/accounts", f"同工名單已經有「{same.name}」（名字或其他寫法是「{name}」），"
                                                      "是同一個人的話請按「對應」", "error")
            line_name = f"LINE 名稱：{person['display_name']}；" if person["display_name"] else ""
            todo = "" if claimed else "，確認真實姓名後改名並勾選「還在服事」"
            items.append(Member(name=name, line_user_id=user_id, active=bool(claimed),
                                note=f"{line_name}{dt.date.today():%m/%d} 從 LINE 帳號加入{todo}"))
            table.save(items)
        m.service.history.clear_real_name(user_id)
        if claimed:
            return m.redirect("/members/accounts", f"已把「{name}」加進同工名單")
        return m.redirect(f"/members?edit={quote(name)}", f"已加入「{name}」（先停用），請把名字改成真實姓名再啟用")

    @ui.post("/members/accounts/link")
    def account_link(user_id: str = Form(...), member_name: str = Form(...), m: MinistryView = Depends(web.ministry)):
        with TABLE_WRITE_LOCK:
            table = MemberTable(m.paths.members_file)
            items = table.load().items
            target = next((x for x in items if x.name == member_name), None)
            if any(x.line_user_id == user_id for x in items):
                return m.redirect("/members/accounts", "這個 LINE 帳號已經對應到同工了", "warn")
            if target is None:
                return m.redirect("/members/accounts", f"同工名單裡找不到「{member_name}」", "error")
            if normalize_name(member_name) in duplicate_names(items):
                return m.redirect("/members/accounts", f"同工名單裡有兩列都叫「{member_name}」，分不出是哪一位；"
                                                      "請先到「名單」把多的那一列刪掉或改名", "error")
            if target.line_user_id:
                return m.redirect("/members/accounts", f"「{member_name}」已經對應到另一個 LINE 帳號，請先確認是不是同一個人",
                                 "error")
            table.save([replace(x, line_user_id=user_id) if x is target else x for x in items])
        m.service.history.clear_real_name(user_id)
        return m.redirect("/members/accounts", f"已把 LINE 帳號對應到「{member_name}」")

    @ui.post("/members/accounts/link-all")
    def account_link_all(m: MinistryView = Depends(web.ministry)):
        """「/點名」之後很多人一起登記：本人登記的名字剛好是名單上一位還沒有 LINE 帳號的同工，一次全部對應。
        （還是管理員按的；撞名、名單上沒有、那位已經有帳號的，照舊一個一個處理。）"""
        with TABLE_WRITE_LOCK:
            table = MemberTable(m.paths.members_file)
            items = table.load().items
            # 用「哪一列」（物件本身）對應，不用名字：名單裡就算有同名的兩列也只會改到那一列
            linkable = exact_links(build_accounts(m.service.history.people(), items), items)
            links = {id(a.match): a.user_id for a in linkable}
            if not links:
                return m.redirect("/members/accounts", "沒有可以一次對應的帳號（可能已經處理好了）", "warn")
            table.save([replace(x, line_user_id=links[id(x)]) if id(x) in links else x for x in items])
        for a in linkable:
            m.service.history.clear_real_name(a.user_id)
        names = "、".join(a.match.name for a in linkable)  # type: ignore[union-attr]
        return m.redirect("/members/accounts", f"已對應 {len(links)} 位：{names}。之後的提醒就 @ 得到他們了")

    @ui.post("/members/accounts/rename")
    def account_rename(user_id: str = Form(...), m: MinistryView = Depends(web.ministry)):
        new_name = (m.service.history.person(user_id) or {}).get("real_name", "")
        if not new_name:
            return m.redirect("/members/accounts", "這個帳號沒有登記新的名字", "warn")
        with TABLE_WRITE_LOCK:
            table = MemberTable(m.paths.members_file)
            items = table.load().items
            current = next((x for x in items if x.line_user_id == user_id), None)
            if current is None:
                return m.redirect("/members/accounts", "這個 LINE 帳號還沒對應到同工，請用「加入」或「對應」", "warn")
            if (other := Directory([x for x in items if x is not current]).lookup(new_name)) is not None:
                return m.redirect("/members/accounts", f"「{new_name}」已經是同工「{other.name}」的名字或其他寫法，"
                                                      "請先確認是不是同一個人", "error")
            # 舊名字留在「其他寫法」：服事表還沒改過來的地方一樣對得到
            aliases = tuple(a for a in dict.fromkeys((*current.aliases, current.name))
                            if normalize_name(a) != normalize_name(new_name))
            table.save([replace(x, name=new_name, aliases=aliases) if x is current else x for x in items])
        m.service.history.clear_real_name(user_id)
        return m.redirect("/members/accounts", f"已把「{current.name}」改名成「{new_name}」（舊名字留在「其他寫法」）")

    @ui.post("/members/accounts/nickname")
    def account_nickname(user_id: str = Form(...), nickname: str = Form(...), m: MinistryView = Depends(web.ministry)):
        """本人用「/我的暱稱」登記的稱呼：加進他對應的同工的「其他寫法」。"""
        with TABLE_WRITE_LOCK:
            table = MemberTable(m.paths.members_file)
            items = table.load().items
            member = next((x for x in items if x.line_user_id == user_id), None)
            if member is None:
                return m.redirect("/members/accounts", "這個 LINE 帳號還沒對應到同工，請先按「加入」或「對應」", "warn")
            if (owner := Directory([x for x in items if x is not member]).lookup(nickname)) is not None:
                return m.redirect("/members/accounts", f"「{nickname}」已經是同工「{owner.name}」的名字或其他寫法，"
                                                     "請先確認是不是同一個人", "error")
            if not any(normalize_name(a) == normalize_name(nickname) for a in (member.name, *member.aliases)):
                table.save([replace(x, aliases=(*x.aliases, nickname)) if x is member else x for x in items])
        m.service.history.drop_nickname(user_id, nickname)
        return m.redirect("/members/accounts", f"已把「{nickname}」加成「{member.name}」的其他寫法")

    @ui.post("/members/accounts/nickname/drop")
    def account_nickname_drop(user_id: str = Form(...), nickname: str = Form(...), m: MinistryView = Depends(web.ministry)):
        m.service.history.drop_nickname(user_id, nickname)
        return m.redirect("/members/accounts", f"已忽略登記的暱稱「{nickname}」（同工名單不變）")

    @ui.post("/members/accounts/ignore")
    def account_ignore(user_id: str = Form(...), m: MinistryView = Depends(web.ministry)):
        m.service.history.clear_real_name(user_id)
        m.service.history.drop_nickname(user_id)  # 登記的暱稱一起放掉，不然還是會一直排在待處理
        linked = any(x.line_user_id == user_id for x in MemberTable(m.paths.members_file).load().items)
        if linked:  # 已經是同工：只是不採用這次登記的名字，帳號本身照樣列出來
            return m.redirect("/members/accounts", "已略過這次登記的名字，同工名單不變")
        m.service.history.set_person_ignored(user_id, True)
        return m.redirect("/members/accounts", "已忽略這個帳號（本人重新登記名字時才會再出現）")

    @ui.post("/members/accounts/unignore")
    def account_unignore(user_id: str = Form(...), m: MinistryView = Depends(web.ministry)):
        m.service.history.set_person_ignored(user_id, False)
        return m.redirect("/members/accounts", "已取消忽略")

    @ui.post("/members/accounts/forget")
    def account_forget(user_id: str = Form(...), m: MinistryView = Depends(web.ministry)):
        m.service.history.forget_person(user_id)
        return m.redirect("/members/accounts", "已刪除這個帳號的紀錄（他之後在群組講話還是會再被記下來）")

    @ui.post("/members/collect")
    def members_collect(enabled: str = Form(""), m: MinistryView = Depends(web.ministry)):
        on = enabled == "1"

        def change(settings: Settings) -> None:
            settings.chat.collect_names = on

        update_settings(m.paths, change)
        if on:
            return m.redirect("/members/accounts", "已開放名字登記：請大家在 LINE 打「/我的名字 真實姓名」。收集完記得關掉")
        return m.redirect("/members/accounts", "已關閉名字登記")

    @ui.post("/members/alias")
    def members_alias(raw: str = Form(...), member_name: str = Form(""), m: MinistryView = Depends(web.ministry)):
        raw = raw.strip()
        with TABLE_WRITE_LOCK:
            table = MemberTable(m.paths.members_file)
            items = table.load().items
            if not member_name:  # 新增成一位新同工
                items = upsert(items, Member(name=raw), key=lambda x: x.name)
                msg = f"已新增同工「{raw}」"
            else:
                items = [replace(x, aliases=(*x.aliases, raw) if raw not in x.aliases else x.aliases)
                         if x.name == member_name else x for x in items]
                msg = f"已把「{raw}」設成「{member_name}」的其他寫法"
            table.save(items)
        return m.redirect("/members", msg)

    # ------------------------------------------------------------------ 小團（config/teams.csv）

    @ui.post("/teams/save")
    def teams_save(name: str = Form(""), aliases: str = Form(""), members: str = Form(""),
                   active: str = Form(""), note: str = Form(""), original_name: str = Form(""), m: MinistryView = Depends(web.ministry)):
        name = name.strip()
        if not name:
            return m.redirect("/members/teams", "請填小團名稱", "error")
        team = Team(name=name, aliases=parse_list(aliases), members=parse_list(members),
                    active=active == "on", note=note.strip())
        directory = Directory(MemberTable(m.paths.members_file).load().items)
        back = m.url(f"/members/teams?edit_team={quote(original_name)}" if original_name else "/members/teams?new=1")
        if strangers := [x for x in team.members if directory.lookup(x) is None]:  # 成員只能是同工名單上的人
            return redirect(back, f"「{'、'.join(strangers)}」不在同工名單上，先到「名單」新增再加進小團", "error")
        for key in (team.name, *team.aliases):  # 團名撞到人名 → 服事表寫這個只會對到那個人
            if (owner := directory.lookup(key)) is not None:
                return m.redirect("/members/teams", f"「{key}」已經是同工「{owner.name}」的名字或其他寫法，"
                                                   "服事表寫這個只會對到那個人，請換一個寫法", "error")
        with TABLE_WRITE_LOCK:
            table = TeamTable(m.paths.teams_file)
            table.save(upsert(table.load().items, team, key=lambda t: t.name, original_key=original_name or None))
        return m.redirect("/members/teams", f"已儲存小團「{name}」")

    @ui.post("/teams/delete")
    def teams_delete(name: str = Form(...), m: MinistryView = Depends(web.ministry)):
        with TABLE_WRITE_LOCK:
            table = TeamTable(m.paths.teams_file)
            table.save(remove(table.load().items, name, key=lambda t: t.name))
        return m.redirect("/members/teams", f"已刪除小團「{name}」")

    # ------------------------------------------------------------------ settings

    def settings_page_response(request: Request, m: MinistryView, values: dict[str, Any] | None = None, error: str = "",
                               broken: str = "") -> HTMLResponse:
        if values is None:
            values = form_values(Settings()) if broken else form_values(load_settings(m.paths))
        return web.page(request, "settings.html", m, f=values, error=error, broken=broken,
                        default_template=DEFAULT_TEMPLATE, ministry=m.unit.ministry,
                        can_delete=len(web.church.ministries()) > 1 and web.is_manager(request))

    @ui.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, m: MinistryView = Depends(web.ministry)):
        try:
            return settings_page_response(request, m)
        except ConfigError as exc:
            return settings_page_response(request, m, broken=f"{exc.message}（{exc.hint}）")

    @ui.post("/settings", response_class=HTMLResponse)
    async def settings_save(request: Request, m: MinistryView = Depends(web.ministry)):
        form = {k: str(v) for k, v in (await request.form()).items()}
        with SETTINGS_LOCK:
            try:
                current = load_settings(m.paths)
            except ConfigError:
                current = Settings()  # 設定檔壞了：用預設值為底，存檔後就修好了
            try:
                new = apply_form(current, form)
            except ChurchBotError as exc:
                return settings_page_response(request, m, values={**form_values(current), **form},
                                              error=f"{exc.message}　{exc.hint}".strip())
            save_settings(m.paths, new)
        await run_in_threadpool(web.scheduler.reload)
        return m.redirect("/settings", f"設定已儲存。自動發送：{web.scheduler.status_for(m.id)}")

    # --- 這個牧區本身：名稱、備註、第二層密碼、移除（存在 church.yaml，見 church.py） ---

    @ui.post("/ministry/save")
    def ministry_save(name: str = Form(""), note: str = Form(""), m: MinistryView = Depends(web.ministry)):
        try:
            renamed = edit_ministry(web.paths, m.id, name=name, note=note)
        except ConfigError as exc:
            return m.redirect("/settings#ministry", f"{exc.message}。{exc.hint}".rstrip("。"), "error")
        return m.redirect("/settings#ministry", f"已儲存：{renamed.name}")

    @ui.post("/ministry/password")
    def ministry_password(password: str = Form(""), clear: str = Form(""), m: MinistryView = Depends(web.ministry)):
        """第二層密碼。設好之後這個瀏覽器直接記住（不用馬上再輸入一次），其他人進這個牧區要輸入。"""
        if clear == "on":
            edit_ministry(web.paths, m.id, password_hash="")
            resp = m.redirect("/settings#ministry", "已取消這個牧區的密碼：只要網站密碼就進得去")
            resp.delete_cookie(ministry_cookie(m.id))
            return resp
        password = password.strip()
        if len(password) < MIN_MINISTRY_PASSWORD:
            return m.redirect("/settings#ministry", f"牧區密碼至少 {MIN_MINISTRY_PASSWORD} 個字", "error")
        updated = edit_ministry(web.paths, m.id, password_hash=hash_password(password))
        resp = m.redirect("/settings#ministry", "已設定牧區密碼：其他人進這個牧區要先輸入（這個瀏覽器已經記住了）")
        resp.set_cookie(ministry_cookie(m.id), ministry_token(updated.password_hash), httponly=True, samesite="lax",
                        max_age=60 * 60 * 24 * 30)
        return resp

    @ui.post("/ministry/delete", dependencies=[Depends(web.require_manager)])
    async def ministry_delete(confirm: str = Form(""), m: MinistryView = Depends(web.ministry)):
        """移除牧區（只有伺服器管理員）：資料夾整包移到 config/_deleted/，發送紀錄留著；要打牧區名稱確認，避免手滑。"""
        if confirm.strip() != m.name:
            return m.redirect("/settings#danger", f"要移除請在框裡打「{m.name}」", "error")
        if len(web.church.ministries()) <= 1:
            return m.redirect("/settings#danger", "這是最後一個牧區，不能移除", "error")
        moved = remove_ministry(web.paths, m.id)
        await run_in_threadpool(web.scheduler.reload)
        return redirect("/", f"已移除「{m.name}」。資料沒有刪掉，搬到 {moved.relative_to(web.paths.root).as_posix()}")

    # ------------------------------------------------------------------ 提醒訊息的即時預覽

    @api.post("/message-preview", summary="編輯提醒訊息時的即時預覽（還沒存的內容也可以；不會送出）")
    async def api_message_preview(request: Request, m: MinistryView = Depends(web.ministry)):
        """設定頁「訊息長什麼樣子」和 LINE 群組的編輯抽屜，打字的時候 app.js 把整張表單送來這裡。

        ``preview_kind=target``：某個群組（空白的部分用牧區設定）；其他：牧區設定本身。
        """
        form = {k: str(v) for k, v in (await request.form()).items()}
        try:
            base = load_settings(m.paths).message
        except ConfigError:
            base = Settings().message
        template = form.get("template", "").replace("\r\n", "\n").strip()
        target = None
        try:
            if form.get("preview_kind") == "target":
                message = base
                target = Target(name=form.get("name", "").strip() or "這個群組", line_id="C" + "0" * 32,
                                roles=parse_list(form.get("roles", "")), labels=parse_list(form.get("labels", "")),
                                title=form.get("title", "").strip(), footer=form.get("footer", "").strip(),
                                template=template)
            else:
                updates: dict[str, Any] = {k: form[k].strip() for k in ("title", "footer") if k in form}
                updates["template"] = template or DEFAULT_TEMPLATE
                if form.get("date_format", "").strip():
                    updates["date_format"] = form["date_format"].strip()
                if "role_order" in form:
                    updates["role_order"] = list(parse_list(form["role_order"]))
                if form.get("name_separator"):
                    updates["name_separator"] = form["name_separator"]
                message = type(base).model_validate({**base.model_dump(), **updates})
            text, note = await run_in_threadpool(m.service.sample_message, message, target)
        except (ChurchBotError, ValidationError) as exc:
            detail = f"{exc.message}。{exc.hint}".rstrip("。") if isinstance(exc, ChurchBotError) else "有欄位格式不對"
            return {"text": "", "note": "", "error": detail}
        return {"text": text, "note": note, "error": ""}

    # ------------------------------------------------------------------ 變更紀錄（見 core/versions.py）

    @ui.get("/history", response_class=HTMLResponse)
    def history_page(request: Request, m: MinistryView = Depends(web.ministry)):
        return web.history_page(request, m)

    @ui.post("/history/{change_id}/restore")
    async def history_restore(change_id: int, m: MinistryView = Depends(web.ministry)):
        msg, level = await run_in_threadpool(web.restore, change_id, m.id)
        return m.redirect("/history", msg, level)

    # ------------------------------------------------------------------ history & help

    @ui.get("/runs", response_class=HTMLResponse)
    def runs_page(request: Request, m: MinistryView = Depends(web.ministry)):
        return web.page(request, "runs.html", m, runs=m.service.history.recent_runs(50))

    @ui.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_detail(request: Request, run_id: str, m: MinistryView = Depends(web.ministry)):
        report = m.service.history.get_report(run_id)
        if report is None:
            raise HTTPException(404, "找不到這筆紀錄")
        return web.page(request, "run_detail.html", m, r=report)

    @ui.get("/help", response_class=HTMLResponse)
    def help_page(request: Request, m: MinistryView = Depends(web.ministry)):
        return web.page(request, "help.html", m)

    @ui.get("/download/{name}")
    def download(name: str, m: MinistryView = Depends(web.ministry)):
        files = {"targets.csv": m.paths.targets_file, "members.csv": m.paths.members_file,
                 "teams.csv": m.paths.teams_file}
        if name not in files or not files[name].exists():
            raise HTTPException(404, "找不到檔案")
        return FileResponse(files[name], filename=name, media_type="text/csv")

    # ------------------------------------------------------------------ JSON API

    def report_json(report: RunReport) -> dict[str, Any]:
        return {**jsonable_encoder(report), "status": report.status}

    @api.get("/status", summary="目前狀態")
    def api_status(m: MinistryView = Depends(web.ministry)):
        last = m.service.history.last_run()
        return {"version": __version__, "schedule": web.scheduler.status_for(m.id),
                "next_run": web.scheduler.next_run(m.id).isoformat() if web.scheduler.next_run(m.id) else None,
                "last_run": jsonable_encoder(last) if last else None}

    @api.get("/preview", summary="預覽這次會發的訊息（不會送出）")
    def api_preview(m: MinistryView = Depends(web.ministry)):
        report, _ = m.service.preview()
        return report_json(report)

    @api.post("/send", summary="立刻發送一次")
    def api_send(force: bool = False, m: MinistryView = Depends(web.ministry)):
        report, _ = m.service.run("manual", force=force)
        return report_json(report)

    @api.get("/check", summary="健康檢查")
    def api_check(m: MinistryView = Depends(web.ministry)):
        return jsonable_encoder(m.service.health())
    return ui, api
