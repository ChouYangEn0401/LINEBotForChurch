"""執行發送計畫：防重複 → 檢查額度 → 送出 → 記錄每一則的結果。

任何一則失敗都會變成 ERROR issue（誰、哪一天、為什麼、怎麼辦），不會只寫在 log 裡。
"""

from __future__ import annotations

import logging

from church_bot.config import BehaviorSettings
from church_bot.core.dates import format_date
from church_bot.core.history import History
from church_bot.core.planner import DATE_FMT, PlannedMessage, Plan
from church_bot.errors import ChurchBotError, MessengerError
from church_bot.messengers.base import Messenger
from church_bot.models import Delivery, DeliveryStatus, Issue, RunReport, Severity, tagged

log = logging.getLogger(__name__)

QUOTA_HINT = "推播到群組是按「群組人數」計算。可到 LINE 官方帳號後台升級方案或等下個月，詳見 docs/LINE_PRICING.md。"
PUSH_TAG = "🤖 自動發送"  # 讓群組裡看得出這則是排程 Push（計費）送的，跟手動 /提醒（免費）分開


def _is_fatal(exc: MessengerError) -> bool:
    """這種錯誤代表後面每一則也一定失敗（token 錯、額度用完），就不要再一直打 LINE 了。"""
    return exc.status_code == 401 or (exc.status_code == 429 and not exc.retryable)


class Dispatcher:
    def __init__(
        self,
        history: History,
        behavior: BehaviorSettings,
        messenger: Messenger | None,
        messenger_error: ChurchBotError | None = None,
        check_quota: bool = True,
    ) -> None:
        self.history = history
        self.behavior = behavior
        self.messenger = messenger
        self.messenger_error = messenger_error
        self.check_quota = check_quota

    def run(self, plan: Plan, report: RunReport, *, dry_run: bool, force: bool) -> None:
        to_send: list[PlannedMessage] = []
        for pm in plan.messages:
            reason = None if force else self.history.skip_reason(
                pm.target.line_id, pm.day.date, pm.day.label, pm.fingerprint, self.behavior.resend_if_changed
            )
            if reason:
                report.deliveries.append(self._delivery(pm, DeliveryStatus.SKIPPED, f"{reason}（要再送請按「重新發送」）"))
            else:
                to_send.append(pm)

        if to_send and self.messenger is not None and self.check_quota:
            self._check_quota(to_send, report)

        if dry_run:
            report.deliveries.extend(self._delivery(pm, DeliveryStatus.DRY_RUN) for pm in to_send)
            return
        if not to_send:
            return
        if self.messenger is None:
            err = self.messenger_error or ChurchBotError("沒有可用的發送方式")
            report.issues.append(Issue(Severity.ERROR, "messenger_unavailable", f"訊息沒有送出：{err.message}", err.hint))
            report.deliveries.extend(self._delivery(pm, DeliveryStatus.FAILED, err.message) for pm in to_send)
            return

        abort: MessengerError | None = None
        for pm in to_send:
            if abort is not None:
                report.deliveries.append(self._delivery(pm, DeliveryStatus.FAILED, f"沒有嘗試：{abort.message}"))
                continue
            when = format_date(pm.day.date, DATE_FMT)
            try:
                result = self.messenger.send(pm.target.line_id, tagged(pm.message, PUSH_TAG))
            except MessengerError as exc:
                log.error("送到 %s 失敗：%s", pm.target.name, exc)
                report.deliveries.append(self._delivery(pm, DeliveryStatus.FAILED, str(exc)))
                report.issues.append(Issue(Severity.ERROR, "send_failed",
                                           f"「{pm.target.name}」沒有收到 {when} 的提醒：{exc.message}", exc.hint))
                if _is_fatal(exc):
                    abort = exc
                continue
            log.info("已送出 %s 的提醒到 %s（Push，計入 LINE 額度）", when, pm.target.name)
            report.deliveries.append(self._delivery(pm, DeliveryStatus.SENT, result.note))
            if result.note:
                severity = Severity.WARNING if result.warn else Severity.INFO
                report.issues.append(Issue(severity, "send_note", f"「{pm.target.name}」：{result.note}"))

    def _check_quota(self, to_send: list[PlannedMessage], report: RunReport) -> None:
        assert self.messenger is not None
        try:
            quota = self.messenger.quota()
            if quota is None:
                return
            sizes: dict[str, int | None] = {}
            for pm in to_send:
                if pm.target.line_id not in sizes:
                    sizes[pm.target.line_id] = self.messenger.audience_size(pm.target.line_id)
        except ChurchBotError as exc:
            report.issues.append(Issue(Severity.WARNING, "quota_unknown", f"查不到本月 LINE 額度：{exc.message}", exc.hint))
            return
        need = sum(sizes[pm.target.line_id] or 0 for pm in to_send)
        report.issues.append(Issue(Severity.INFO, "quota", f"LINE 額度：{quota.describe()}；這次預計用掉約 {need} 則"))
        if quota.remaining is not None and need > quota.remaining:
            report.issues.append(Issue(
                Severity.WARNING, "quota_low",
                f"本月 LINE 額度只剩 {quota.remaining} 則，這次預計需要 {need} 則，可能有群組收不到", QUOTA_HINT,
            ))

    @staticmethod
    def _delivery(pm: PlannedMessage, status: DeliveryStatus, detail: str = "") -> Delivery:
        return Delivery(
            target_name=pm.target.name, target_id=pm.target.line_id, service_date=pm.day.date,
            text=tagged(pm.message, PUSH_TAG).text, status=status, detail=detail, label=pm.day.label,
            fingerprint=pm.fingerprint,
        )
