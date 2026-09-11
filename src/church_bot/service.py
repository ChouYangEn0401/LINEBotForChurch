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
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from church_bot.config import Paths, Settings, load_settings
from church_bot.core.dates import format_date
from church_bot.core.directory import Directory
from church_bot.core.dispatcher import Dispatcher
from church_bot.core.history import History
from church_bot.core.parser import parse_roster
from church_bot.core.planner import DATE_FMT, Plan, Planner, find_unknown_names
from church_bot.core.renderer import Renderer
from church_bot.errors import ChurchBotError, SourceError
from church_bot.messengers import Messenger, build_messenger
from church_bot.models import Issue, Member, OutgoingMessage, Roster, RunReport, Severity, Target
from church_bot.sources import build_source
from church_bot.tables import LINE_ID_RE, MemberTable, TargetTable

log = logging.getLogger(__name__)

ROSTER_CACHE_SECONDS = 60
TRIGGER_ZH = {"schedule": "自動排程", "manual": "網頁手動", "cli": "指令", "catchup": "開機補發", "preview": "預覽"}


@dataclass(slots=True)
class Context:
    """一次執行需要的所有設定與對照表（都是當下重新讀檔的結果）。"""

    settings: Settings
    targets: list[Target]
    members: list[Member]
    issues: list[Issue] = field(default_factory=list)

    @property
    def directory(self) -> Directory:
        return Directory(self.members)


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
    def __init__(self, paths: Paths) -> None:
        self.paths = paths
        self.history = History(paths.db_file)
        self._run_lock = threading.Lock()  # 排程和手動按鈕同時按下去也不會重複發送
        self._cache_lock = threading.Lock()
        self._roster_cache: tuple[str, float, Roster] | None = None

    # ------------------------------------------------------------------ loading

    def load(self) -> Context:
        settings = load_settings(self.paths)
        issues: list[Issue] = []
        targets = self._load_table(TargetTable(self.paths.targets_file), issues)
        members = self._load_table(MemberTable(self.paths.members_file), issues)
        Renderer(settings.message)  # 模板語法錯誤在這裡就丟 ConfigError
        span = settings.schedule.every_n_weeks * 7
        if settings.schedule.every_n_weeks > 1 and settings.behavior.lookahead_days < span:
            issues.append(Issue(
                Severity.WARNING, "lookahead_too_short",
                f"設定成每 {settings.schedule.every_n_weeks} 週發送一次，但「往後看幾天」只有"
                f"{settings.behavior.lookahead_days} 天，下一次發送前那幾週的服事可能不會出現在提醒裡",
                f"到「設定 → ⑤ 進階」把「往後看幾天」改成至少 {span} 天。",
            ))
        return Context(settings, targets, members, issues)

    @staticmethod
    def _load_table(table: TargetTable | MemberTable, issues: list[Issue]) -> list:
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

    def run(self, trigger: str, *, dry_run: bool = False, force: bool = False,
            use_cache: bool = False) -> tuple[RunReport, Plan | None]:
        with self._run_lock:
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
                if not dry_run and ctx is not None:
                    self._alert_admin(ctx.settings, messenger, report)
                if not dry_run:  # 預覽不記錄：網頁每次打開都會預覽，記下來只會讓資料庫一直變大
                    self._record(report)
                if messenger is not None:
                    messenger.close()
            self._log_summary(report)
            return report, plan

    def preview(self) -> tuple[RunReport, Plan | None]:
        return self.run("preview", dry_run=True, use_cache=True)

    def _alert_admin(self, settings: Settings, messenger: Messenger | None, report: RunReport) -> None:
        if not (report.has_errors or report.has_warnings):
            return
        admin = settings.line.admin_target_id
        if not admin:
            report.issues.append(Issue(Severity.WARNING, "no_admin", "有問題需要處理，但沒有設定「管理員」，所以沒辦法用 LINE 通知你",
                                       "到「設定」頁填管理員的 LINE ID（私訊機器人「我的ID」就能拿到）。"))
            return
        if messenger is None:
            log.error("有問題需要通知管理員，但目前沒有可用的發送方式")
            return
        try:
            messenger.send(admin, OutgoingMessage(text=build_admin_alert(report)))
            log.info("已通知管理員")
        except ChurchBotError as exc:
            log.error("通知管理員失敗：%s", exc)
            report.issues.append(Issue(Severity.ERROR, "admin_alert_failed", f"通知管理員失敗：{exc.message}", exc.hint))

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
        items.append(CheckItem("群組表", bool(active) and not table_errors,
                               f"共 {len(ctx.targets)} 個群組，{len(active)} 個會收到提醒",
                               "；".join(i.message for i in table_errors) or ("請到「群組」頁新增群組" if not active else "")))
        items.append(CheckItem("人員表", None if not ctx.members else True,
                               f"共 {len(ctx.members)} 位" if ctx.members else "還沒設定（名字會照服事表原樣顯示）"))

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
                        low = quota.remaining is not None and quota.remaining < 50
                        items.append(CheckItem("LINE 本月額度", not low, quota.describe(),
                                               "額度快用完了，詳見 docs/LINE_PRICING.md" if low else ""))
                finally:
                    messenger.close()
            except ChurchBotError as exc:
                items.append(CheckItem("LINE 連線", False, exc.message, exc.hint))

        admin = s.line.admin_target_id
        items.append(CheckItem("管理員通知", bool(admin and LINE_ID_RE.match(admin)),
                               f"出問題會通知：{admin}" if admin else "還沒設定，出問題時沒辦法用 LINE 通知你",
                               "" if admin else "私訊機器人「我的ID」，把拿到的 ID 填到「設定 → 管理員 LINE ID」。"))
        items.append(CheckItem("自動排程", s.schedule.enabled or None, s.schedule.describe()))
        items.append(CheckItem("群組 ID 自動抓取", True if s.line.channel_secret else None,
                               "已設定 Channel secret" if s.line.channel_secret else "沒有設定 Channel secret（選用功能）",
                               "" if s.line.channel_secret else "只有想用「在群組打 群組ID 自動抓」才需要，見 docs/SETUP_LINE.md。"))
        return items
