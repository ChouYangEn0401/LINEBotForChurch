"""把所有零件組起來的地方：設定 → 對照表 → 服事表 → 規劃 → 發送 → 記錄 → 通知管理員。

每次執行都重新讀設定與 CSV，所以在 Excel 改完對照表、在網頁改完設定，不用重開程式。
網頁、排程、指令列都只跟這個類別打交道。
"""

from __future__ import annotations

import datetime as dt
import logging
import secrets
import threading
import time
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from typing import Callable
from zoneinfo import ZoneInfo

from church_bot.config import Paths, Settings, load_settings
from church_bot.core.dates import format_date
from church_bot.core.directory import Directory, validate_teams
from church_bot.core.dispatcher import Dispatcher
from church_bot.core.history import History
from church_bot.core.parser import SheetInfo, inspect_sheet, parse_roster
from church_bot.core.planner import DATE_FMT, Plan, Planner, active_targets, find_unknown_names
from church_bot.core.public_url import ServiceUrl, load_service_url, save_service_url
from church_bot.core.quota import QuotaSnapshot, load_quota, save_quota
from church_bot.core.renderer import Renderer
from church_bot.errors import ChurchBotError, SourceError
from church_bot.locking import process_lock
from church_bot.messengers import Messenger, build_messenger
from church_bot.models import (
    TRIGGER_ZH, Delivery, DeliveryStatus, Issue, Member, OutgoingMessage, RawSheet, Roster, RunReport, Severity,
    Target, Team, tagged,
)
from church_bot.sources import build_source
from church_bot.tables import LINE_ID_RE, MemberTable, TargetTable, TeamTable

log = logging.getLogger(__name__)

ROSTER_CACHE_SECONDS = 60
QUOTA_LOW_THRESHOLD = 50
REPLY_LIMIT = 4  # 一次 Reply 最多 5 則泡泡，留 1 則給確認訊息
# 「等一下再試可能就好」的問題：讀不到服事表（網路、Google）、LINE 暫時連不上、沒預料到的錯誤。
# 設定錯、沒有群組、LINE 拒絕（機器人被踢、額度用完）這種，重試也沒用。
RETRYABLE_CODES = frozenset({"SourceError", "unexpected", "send_failed_temporarily"})
REPLY_TAG = "🙋 手動發送・免費"  # 讓群組裡看得出這則是用 /提醒（Reply，免費）送的，跟排程 Push 分開
TEST_TAG = "🧪 測試預覽・沒有真的發送"  # /別周測試：管理員試看別一週的內容
NOT_A_TARGET = "這個聊天室目前不是設定好的提醒群組，要先到管理網頁「LINE 群組」頁新增、啟用才能用這個指令。"


@dataclass(slots=True)
class Context:
    """一次執行需要的所有設定與對照表（都是當下重新讀檔的結果）。"""

    settings: Settings
    targets: list[Target]
    members: list[Member]
    teams: list[Team] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    @property
    def directory(self) -> Directory:
        return Directory(self.members, self.teams)


@dataclass(frozen=True, slots=True)
class CheckItem:
    name: str
    ok: bool | None  # None = 略過 / 僅供參考
    detail: str
    hint: str = ""

    @property
    def icon(self) -> str:
        return {True: "✅", False: "❌", None: "➖"}[self.ok]


def new_run_id() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)


def worth_retrying(report: RunReport) -> bool:
    return any(i.code in RETRYABLE_CODES for i in report.issues if i.is_error)


def build_admin_alert(report: RunReport, limit: int = 8) -> str:
    problems = [i for i in report.issues if i.severity is not Severity.INFO]
    head = "❌ 服事提醒機器人：這次執行有錯誤" if report.has_errors else "⚠️ 服事提醒機器人：有事項需要處理"
    stats = f"已送出 {report.count_sent} 則、失敗 {report.count_failed} 則"
    lines = [head, f"（{TRIGGER_ZH.get(report.trigger, report.trigger)}・{stats}）", ""]
    lines += [i.one_line() for i in problems[:limit]]
    if len(problems) > limit:
        lines.append(f"…還有 {len(problems) - limit} 項")
    lines += ["", "詳細說明請打開管理網頁查看。"]
    return "\n".join(lines)


class BotService:
    def __init__(self, paths: Paths, shared: History | None = None) -> None:
        """``shared`` = 整個教會共用的資料庫（LINE 用量、對外網址）；沒給就跟自己的同一份（只有一個牧區時）。"""
        self.paths = paths
        self.history = History(paths.db_file)
        self.shared = shared or self.history
        self._run_lock = threading.Lock()  # 排程和手動按鈕同時按下去也不會重複發送
        self._cache_lock = threading.Lock()
        self._roster_cache: tuple[str, float, Roster] | None = None

    # ------------------------------------------------------------------ loading

    def load(self) -> Context:
        settings = load_settings(self.paths)
        issues: list[Issue] = []
        targets = self._load_table(TargetTable(self.paths.targets_file), issues)
        members = self._load_table(MemberTable(self.paths.members_file), issues)
        teams = self._load_table(TeamTable(self.paths.teams_file), issues)
        issues.extend(validate_teams(teams, members))
        Renderer(settings.message)  # 模板語法錯誤在這裡就丟 ConfigError
        span = settings.schedule.every_n_weeks * 7
        if settings.schedule.every_n_weeks > 1 and settings.behavior.lookahead_days < span:
            issues.append(Issue(
                Severity.WARNING, "lookahead_too_short",
                f"設定成每 {settings.schedule.every_n_weeks} 週發送一次，但「往後看幾天」只有"
                f"{settings.behavior.lookahead_days} 天，下一次發送前那幾週的服事可能不會出現在提醒裡",
                f"到「設定 → 發送行為」把「往後看幾天」改成至少 {span} 天。",
            ))
        return Context(settings, targets, members, teams, issues)

    @staticmethod
    def _load_table(table: TargetTable | MemberTable | TeamTable, issues: list[Issue]) -> list:
        try:
            result = table.load()
        except ChurchBotError as exc:
            issues.append(Issue(Severity.ERROR, "table_error", exc.message, exc.hint))
            return []
        issues.extend(result.issues)
        return result.items

    def now(self, settings: Settings) -> dt.datetime:
        return dt.datetime.now(ZoneInfo(settings.schedule.timezone))

    def fetch_roster(self, settings: Settings, today: dt.date, *, use_cache: bool = False) -> Roster:
        key = settings.source.model_dump_json() + today.isoformat()
        if use_cache:
            with self._cache_lock:
                cached = self._roster_cache
            if cached and cached[0] == key and time.monotonic() - cached[1] < ROSTER_CACHE_SECONDS:
                return cached[2]

        sheets = build_source(settings.source, self.paths).fetch()
        rosters: list[Roster] = []
        failures: list[Issue] = []
        for sheet in sheets:
            try:
                rosters.append(parse_roster(sheet, settings.source, today))
            except SourceError as exc:
                if len(sheets) == 1:
                    raise
                failures.append(Issue(Severity.ERROR, "sheet_error", f"{sheet.source}：{exc.message}", exc.hint))
        if not rosters:
            raise SourceError("每一個分頁都讀不懂", failures[0].hint if failures else "")
        roster = rosters[0] if len(rosters) == 1 and not failures else self._merge(rosters, failures)
        with self._cache_lock:
            self._roster_cache = (key, time.monotonic(), roster)
        return roster

    @staticmethod
    def _merge(rosters: list[Roster], issues: list[Issue]) -> Roster:
        days: dict[tuple[dt.date, str], object] = {}
        for roster in rosters:
            issues.extend(roster.issues)
            for day in roster.days:
                key = (day.date, day.label)
                if key in days:
                    issues.append(Issue(Severity.WARNING, "day_duplicate",
                                        f"{format_date(day.date, DATE_FMT)} {day.label} 同時出現在兩個分頁，只採用第一個",
                                        "請把重複的那一份刪掉。"))
                    continue
                days[key] = day
        ordered = tuple(sorted(days.values(), key=lambda d: d.date))  # type: ignore[attr-defined]
        return Roster(days=ordered, source="、".join(r.source for r in rosters), issues=tuple(issues))

    # ------------------------------------------------------------------ running

    def run(self, trigger: str, *, dry_run: bool = False, force: bool = False, use_cache: bool = False,
            will_retry: bool = False) -> tuple[RunReport, Plan | None]:
        """will_retry：呼叫的人等一下會重試（cli send --retries）。這次的問題如果是重試可能就好的，
        先不通知管理員，免得每試一次就扣一則 LINE；最後一次還是失敗才通知。"""
        # 真的發送時要跟其他程式排隊（見 locking.py）；預覽不發送，不用排
        send_lock = nullcontext() if dry_run else process_lock(self.paths.data_dir / "send.lock")
        with self._run_lock, send_lock:
            started = dt.datetime.now().astimezone()
            report = RunReport(run_id=new_run_id(), trigger=trigger, dry_run=dry_run, started_at=started)
            plan: Plan | None = None
            ctx: Context | None = None
            messenger: Messenger | None = None
            try:
                ctx = self.load()
                report.issues.extend(ctx.issues)
                today = self.now(ctx.settings).date()
                roster = self.fetch_roster(ctx.settings, today, use_cache=use_cache)
                planner = Planner(Renderer(ctx.settings.message), ctx.directory, ctx.settings.behavior)
                plan = planner.plan(roster, ctx.targets, today)
                report.service_date = plan.service_date
                report.issues.extend(i for i in plan.issues if i not in report.issues)

                messenger_error: ChurchBotError | None = None
                try:
                    messenger = build_messenger(ctx.settings, self.paths)
                except ChurchBotError as exc:
                    messenger_error = exc  # 正式發送時 Dispatcher 會把每一則標成失敗並說明原因
                    if dry_run:
                        report.issues.append(Issue(Severity.WARNING, "messenger_unavailable",
                                                   f"目前還不能真的發送：{exc.message}", exc.hint))
                Dispatcher(self.history, ctx.settings.behavior, messenger, messenger_error,
                           check_quota=ctx.settings.messenger.check_quota).run(
                    plan, report, dry_run=dry_run, force=force)
            except ChurchBotError as exc:
                report.issues.append(Issue(Severity.ERROR, type(exc).__name__, exc.message, exc.hint))
            except Exception as exc:  # noqa: BLE001 - 沒預料到的錯誤也絕不能靜默
                log.exception("未預期的錯誤")
                report.issues.append(Issue(Severity.ERROR, "unexpected", f"程式發生未預期的錯誤：{exc!r}",
                                           "請把 data/church_bot.log 傳給維護的人。"))
            finally:
                report.finished_at = dt.datetime.now().astimezone()
                if not dry_run and report.count_sent:
                    self._note_push(messenger)  # 用量變了：馬上更新快照，並排一次 5 分鐘後的重查
                if not dry_run and ctx is not None and not (will_retry and worth_retrying(report)):
                    self._alert_admin(ctx.settings, messenger, report)
                if not dry_run:  # 預覽不記錄：網頁每次打開都會預覽，記下來只會讓資料庫一直變大
                    self._record(report)
                if messenger is not None:
                    messenger.close()
            self._log_summary(report)
            return report, plan

    def preview(self) -> tuple[RunReport, Plan | None]:
        return self.run("preview", dry_run=True, use_cache=True)

    def admin_targets(self, settings: Settings, members: list[Member] | None = None) -> list[str]:
        """出問題要通知誰。設定裡填了「出問題通知誰」就只通知那一個（可以是群組）；
        沒填就通知同工名單裡每一位勾了「管理員」而且對應好 LINE 帳號的人（每人各算 1 則）。"""
        explicit = (settings.line.admin_target_id or "").strip()
        if explicit:
            return [explicit]
        if members is None:
            members = self._load_table(MemberTable(self.paths.members_file), [])
        return [m.line_user_id for m in members if m.admin and m.line_user_id]

    def _alert_admin(self, settings: Settings, messenger: Messenger | None, report: RunReport) -> None:
        if not (report.has_errors or report.has_warnings):
            return
        targets = self.admin_targets(settings)
        if not targets:
            report.issues.append(Issue(Severity.WARNING, "no_admin", "有問題需要處理，但還沒有管理員，所以沒辦法用 LINE 通知你",
                                       "到「同工名單」把自己勾成管理員（要先對應好 LINE 帳號），或到「設定 → 出問題通知誰」填 LINE ID。"))
            return
        own: Messenger | None = None
        if messenger is None:  # 例如讀不到服事表：還沒走到建立發送方式那一步就出錯了，這種最需要通知
            try:
                own = messenger = build_messenger(settings, self.paths)
            except ChurchBotError as exc:
                log.error("有問題需要通知管理員，但目前沒有可用的發送方式：%s", exc)
                return
        try:
            for target in targets:
                messenger.send(target, OutgoingMessage(text=build_admin_alert(report)))
            log.info("已通知管理員（%d 位）", len(targets))
        except ChurchBotError as exc:
            log.error("通知管理員失敗：%s", exc)
            report.issues.append(Issue(Severity.ERROR, "admin_alert_failed", f"通知管理員失敗：{exc.message}", exc.hint))
        finally:
            if own is not None:
                own.close()

    def _record(self, report: RunReport) -> None:
        try:
            self.history.record(report)
        except Exception:  # noqa: BLE001
            log.exception("寫入執行紀錄失敗（不影響已送出的訊息）")

    @staticmethod
    def _log_summary(report: RunReport) -> None:
        if report.dry_run:
            log.debug("預覽完成：%s", report.status_zh)
            return
        level = logging.ERROR if report.has_errors else logging.WARNING if report.has_warnings else logging.INFO
        log.log(level, "[%s] %s：送出 %d、失敗 %d、略過 %d、預覽 %d、問題 %d 項", TRIGGER_ZH.get(report.trigger, report.trigger),
                report.status_zh, report.count_sent, report.count_failed, report.count_skipped, report.count_dry_run,
                sum(1 for i in report.issues if i.severity is not Severity.INFO))
        for issue in report.issues:
            if issue.severity is not Severity.INFO:
                log.log(logging.ERROR if issue.is_error else logging.WARNING, "%s", issue.one_line())

    # ------------------------------------------------------------------ helpers for UI

    def unknown_names(self) -> dict[str, tuple[str, ...]]:
        ctx = self.load()
        today = self.now(ctx.settings).date()
        roster = self.fetch_roster(ctx.settings, today, use_cache=True)
        return find_unknown_names(roster, ctx.directory, since=today)

    def clear_roster_cache(self) -> None:
        """「服事表」頁按「重新讀取」：下一次預覽一定重新去 Google 抓，不用等 60 秒。"""
        with self._cache_lock:
            self._roster_cache = None

    def sheet_preview(self) -> tuple[Context, list[RawSheet], list[SheetInfo | None], dt.date]:
        """「服事表」頁用：把資料來源的原始格子原封不動抓回來，並附上「程式是怎麼看它的」。

        讀不到（沒網路、沒開共用、網址錯）一律丟 ChurchBotError，畫面會把原因和解法印出來。
        """
        ctx = self.load()
        today = self.now(ctx.settings).date()
        sheets = build_source(ctx.settings.source, self.paths).fetch()
        return ctx, sheets, [inspect_sheet(sheet, ctx.settings.source, today) for sheet in sheets], today

    def roster_roles(self) -> list[str]:
        """服事表上現有的服事項目（「LINE 群組」頁用來讓人用選的，不用自己猜怎麼寫）。

        讀不到服事表不是問題：那一頁照樣要能用，只是選不了而已。
        """
        try:
            ctx = self.load()
            return self.fetch_roster(ctx.settings, self.now(ctx.settings).date(), use_cache=True).all_roles()
        except ChurchBotError as exc:
            log.debug("讀不到服事表，「LINE 群組」頁就不列出服事項目：%s", exc)
            return []

    # ------------------------------------------------------------------ 本月 LINE 用量（見 core/quota.py）

    def _quota_watched(self) -> bool:
        """要不要顯示用量：測試模式（console）和關掉額度檢查的，主控台那一格就不出現。"""
        try:
            settings = load_settings(self.paths)
        except ChurchBotError:
            return False
        return settings.messenger.kind != "console" and settings.messenger.check_quota

    def quota_status(self) -> QuotaSnapshot | None:
        """主控台用：上次查到的本月用量。不連網，所以畫面一定馬上出來；None = 不適用（見 _quota_watched）。"""
        if not self._quota_watched():
            return None
        return load_quota(self.shared)

    def refresh_quota(self, *, force: bool = False) -> QuotaSnapshot | None:
        """該查的時候向 LINE 問一次用量並存起來（``force`` = 不管該不該，一定重新問）。

        誰會呼叫：主控台載入後由 app.js 問 /api/quota、管理網頁的定時工作、按「重新查詢」。
        問不到不丟例外：原因記在快照裡，畫面顯示舊數字＋為什麼是舊的。
        """
        if not self._quota_watched():
            return None
        snapshot = load_quota(self.shared)
        now = dt.datetime.now().astimezone()
        if not (force or snapshot.due(now)):
            return snapshot
        try:
            messenger = build_messenger(load_settings(self.paths), self.paths)
        except ChurchBotError as exc:
            return save_quota(self.shared, snapshot.failed(exc.message, now))
        try:
            quota = messenger.quota()
        except ChurchBotError as exc:
            log.info("查不到本月 LINE 用量：%s", exc.message)
            return save_quota(self.shared, snapshot.failed(exc.message, now))
        finally:
            messenger.close()
        if quota is None:
            return snapshot
        return save_quota(self.shared, snapshot.updated(quota, now))

    def _note_push(self, messenger: Messenger | None) -> None:
        """剛 Push 完的收尾：馬上更新一次用量，並排一次 SETTLE 之後的重查。

        LINE 的用量統計會延遲幾分鐘，所以「馬上問到的數字」通常還沒算進這一次；
        重查由管理網頁的定時工作做（下次打開管理網頁也會補查），這樣用量不會停在發送前的數字。
        """
        now = dt.datetime.now().astimezone()
        snapshot = load_quota(self.shared)
        if messenger is not None:
            try:
                if (quota := messenger.quota()) is not None:
                    snapshot = snapshot.updated(quota, now)
            except ChurchBotError as exc:
                snapshot = snapshot.failed(exc.message, now)
        save_quota(self.shared, snapshot.dirty(now))

    # ------------------------------------------------------------------ 對外網址（見 core/public_url.py）

    def service_url(self) -> ServiceUrl:
        """目前對外的管理網頁網址（免費模式每次重開都會變）。沒記錄過就是空的。"""
        return load_service_url(self.shared)

    def remember_service_url(self, url: str, source: str = "webhook") -> ServiceUrl:
        return save_service_url(self.shared, url, source)

    def _plan_for(self, ctx: Context, targets: list[Target], today: dt.date) -> Plan:
        roster = self.fetch_roster(ctx.settings, today, use_cache=True)
        planner = Planner(Renderer(ctx.settings.message), ctx.directory, ctx.settings.behavior)
        return planner.plan(roster, targets, today)

    def preview_target(self, target: Target) -> tuple[list[str], str]:
        """「LINE 群組」頁編輯一個群組時，旁邊顯示「這個群組現在會收到什麼」。

        停用的、還沒填 ID 的群組也照樣試排（借一個假的 ID），這樣設定訊息時不用先啟用。
        回傳 (訊息文字, 沒有訊息時的原因)；讀不到服事表也不丟例外，原因寫在第二個值。
        """
        trial = replace(target, enabled=True, line_id=target.line_id if LINE_ID_RE.match(target.line_id)
                        else "C" + "0" * 32)
        try:
            ctx = self.load()
            plan = self._plan_for(ctx, [trial], self.now(ctx.settings).date())
        except ChurchBotError as exc:
            return [], exc.message
        if texts := [pm.message.text for pm in plan.messages]:
            return texts, ""
        reason = next((i.message for i in plan.issues if i.code in ("target_template", "target_nothing")), "")
        return [], reason or "這幾天服事表沒有東西可以提醒。"

    def preview_for(self, day: dt.date, chat_id: str = "") -> tuple[list[OutgoingMessage], str]:
        """給 LINE 指令「/別周測試 10/04」用：試印「那一天起往後幾天」的提醒。

        純預覽：不寫紀錄、不通知管理員，排程時間到了照常 Push（跟 /提醒 不一樣）。
        在提醒群組裡打，就用那個群組的設定（只發哪些服事、要不要 @ 人）；
        在別的地方（例如私訊機器人）打，就借第一個啟用的群組的設定試排，但不會 @ 人
        ——那些人不在這個聊天室裡，LINE 會拒絕整則訊息。
        """
        ctx = self.load()
        notes: list[str] = []
        target = next((t for t in ctx.targets if t.line_id == chat_id and t.enabled), None)
        if target is None:
            if borrowed := next(iter(active_targets(ctx.targets)), None):
                target = replace(borrowed, mention=False)
                notes.append(f"（這裡不是提醒群組，用「{borrowed.name}」的設定試排，而且不會 @ 人）")
        plan = self._plan_for(ctx, [target] if target else [], day)
        span = ctx.settings.behavior.lookahead_days
        window = f"{format_date(day, DATE_FMT)} 起往後 {span} 天"
        if target is None:
            messages = [message for _day, message in plan.samples]
            notes.append("（還沒設定任何提醒群組，這是不分群組的內容）")
        else:
            messages = [pm.message for pm in plan.messages]
        if not messages:
            reason = f"🧪 {window}：服事表裡沒有可以提醒的內容。"
            return [], "\n".join([reason, *notes])
        batch, remainder = messages[:REPLY_LIMIT], messages[REPLY_LIMIT:]
        notes.insert(0, f"🧪 以上是「{window}」的試印結果，沒有發給任何人、也沒有計入紀錄。")
        if remainder:
            notes.append(f"（還有 {len(remainder)} 則沒顯示，一次最多回 5 則。）")
        log.info("管理員用「/別周測試」試印 %s 的提醒（%d 則，沒有送出給大家）", day.isoformat(), len(batch))
        return [tagged(message, f"{TEST_TAG}（{window}）") for message in batch], "\n".join(notes)

    def notify_now(self, chat_id: str, send: Callable[[list[OutgoingMessage | str]], None] | None = None,
                   ) -> tuple[list[OutgoingMessage], str]:
        """給 LINE 聊天指令「/提醒」用：立即用 Reply 免費送出這個群組這次的提醒（不計入 LINE 額度）。

        回傳 (要用 Reply 送出的訊息, 給觸發者看的說明)；訊息是空的代表沒有東西要送，說明會講原因。
        Reply 免費，所以不管之前送過沒都照送（有人問「這週誰服事」就能馬上看到最新的）。
        ``send`` 是真正送出的動作（訊息 + 說明一起）；**送成功才記錄**，送失敗就丟出錯誤、什麼都不記，
        排程時間到了照常 Push。記錄之後，排程在 2 天內（見 `History.skip_reason`）內容沒變就略過，不會重複扣費。
        """
        ctx = self.load()
        target = next((t for t in ctx.targets if t.line_id == chat_id and t.enabled), None)
        if target is None:
            return [], NOT_A_TARGET

        today = self.now(ctx.settings).date()
        plan = self._plan_for(ctx, [target], today)
        to_send = plan.messages
        if not to_send:
            return [], f"這幾天服事表沒有「{target.name}」符合的服事內容，沒有東西可以提醒。"

        batch, remainder = to_send[:REPLY_LIMIT], to_send[REPLY_LIMIT:]
        messages = [tagged(pm.message, REPLY_TAG) for pm in batch]
        note = "✅ 已免費送出（不計入 LINE 額度）"
        if remainder:
            note += f"\n（還有 {len(remainder)} 則這次沒一起送，Reply 一次最多 5 則；剩下的到排程時間會照常送出。）"
        if send is not None:
            send([*messages, note])  # 失敗會丟出錯誤，下面的紀錄就不會寫

        now = dt.datetime.now().astimezone()
        report = RunReport(run_id=new_run_id(), trigger="reply", dry_run=False, started_at=now, finished_at=now,
                           service_date=batch[0].day.date)
        for pm in batch:
            report.deliveries.append(Delivery(
                target_name=pm.target.name, target_id=pm.target.line_id, service_date=pm.day.date,
                text=tagged(pm.message, REPLY_TAG).text, status=DeliveryStatus.SENT,
                detail="LINE 指令「/提醒」送出（免費）", label=pm.day.label, fingerprint=pm.fingerprint,
            ))
        self._record(report)
        log.info("已用「/提醒」免費送出 %d 則提醒到「%s」（Reply，不計入 LINE 額度）", len(batch), target.name)
        return messages, note

    def health(self) -> list[CheckItem]:
        items: list[CheckItem] = []
        try:
            ctx = self.load()
        except ChurchBotError as exc:
            return [CheckItem("設定檔", False, exc.message, exc.hint)]
        s = ctx.settings
        items.append(CheckItem("設定檔", True, "讀取正常"))

        active = [t for t in ctx.targets if t.enabled and LINE_ID_RE.match(t.line_id)]
        table_errors = [i for i in ctx.issues if i.is_error]
        items.append(CheckItem("LINE 群組", bool(active) and not table_errors,
                               f"共 {len(ctx.targets)} 個群組，{len(active)} 個會收到提醒",
                               "；".join(i.message for i in table_errors) or ("請到「LINE 群組」頁新增群組" if not active else "")))
        items.append(CheckItem("同工名單", None if not ctx.members else True,
                               f"共 {len(ctx.members)} 位" if ctx.members else "還沒設定（名字會照服事表原樣顯示）"))
        items.append(CheckItem("小團", None if not ctx.teams else True,
                               f"共 {len(ctx.teams)} 團（服事表寫團名就會列出成員）" if ctx.teams else "沒有使用（選用功能）",
                               "；".join(i.message for i in ctx.issues if i.code.startswith("team_"))))

        today = self.now(s).date()
        try:
            roster = self.fetch_roster(s, today)
            last = format_date(roster.last_date, "%Y/%-m/%-d") if roster.last_date else "（沒有日期）"
            items.append(CheckItem("服事表", bool(roster.days),
                                   f"{roster.source}：{len(roster.days)} 次聚會，排到 {last}",
                                   next((i.hint for i in roster.issues if i.is_error), "")))
        except ChurchBotError as exc:
            items.append(CheckItem("服事表", False, exc.message, exc.hint))

        if s.messenger.kind == "console":
            items.append(CheckItem("LINE 連線", None, "目前是測試模式，訊息只會寫到 data/outbox.log",
                                   "要正式發送請到「設定」把發送方式改成 LINE。"))
        else:
            try:
                messenger = build_messenger(s, self.paths)
                try:
                    items.append(CheckItem("LINE 連線", True, f"機器人：{messenger.check()}"))
                    quota = messenger.quota()
                    if quota is not None:
                        # 系統檢查本來就會問一次，順手存進快照：跑完檢查，主控台那一格就是新的
                        now = dt.datetime.now().astimezone()
                        save_quota(self.shared, load_quota(self.shared).updated(quota, now))
                        low = quota.remaining is not None and quota.remaining < QUOTA_LOW_THRESHOLD
                        items.append(CheckItem("LINE 本月額度", not low, quota.describe(),
                                               "額度快用完了，詳見 docs/LINE_PRICING.md" if low else ""))
                finally:
                    messenger.close()
            except ChurchBotError as exc:
                items.append(CheckItem("LINE 連線", False, exc.message, exc.hint))

        admin = s.line.admin_target_id
        admins = [m.name for m in ctx.members if m.admin and m.line_user_id]
        targets = self.admin_targets(s, ctx.members)
        items.append(CheckItem("管理員通知", bool(targets) and all(LINE_ID_RE.match(t) for t in targets),
                               f"出問題會通知：{admin}" if admin else
                               f"出問題會通知管理員：{'、'.join(admins)}" if admins else
                               "還沒有管理員，出問題時沒辦法用 LINE 通知你",
                               "" if targets else "到「同工名單」把自己勾成管理員（要先對應好 LINE 帳號），或到「設定 → 出問題通知誰」填 LINE ID。"))
        items.append(CheckItem("自動排程", s.schedule.enabled or None, s.schedule.describe()))
        items.append(CheckItem("群組 ID 自動抓取", True if s.line.channel_secret else None,
                               "已設定 Channel secret" if s.line.channel_secret else "沒有設定 Channel secret（選用功能）",
                               "" if s.line.channel_secret else "只有想用「在群組打 /群組ID 自動抓」才需要，見 docs/SETUP_LINE.md。"))
        return items
