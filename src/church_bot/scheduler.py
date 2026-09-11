"""自動排程：在程式裡排程（不用另外設定 Windows「工作排程器」或 macOS 的 cron）。

* 每週固定時間執行一次 ``BotService.run("schedule")``。
* 開機補發：如果排程時間電腦剛好關機 / 睡眠，之後 12 小時內打開程式會自動補發一次。
  已經送過的訊息不會重送（有防重複機制），所以補發很安全。
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from church_bot.config import WEEKDAYS, ScheduleSettings, load_settings
from church_bot.errors import ChurchBotError
from church_bot.service import BotService

log = logging.getLogger(__name__)

JOB_ID = "weekly-reminder"
CATCHUP_HOURS = 12


def previous_fire_time(cfg: ScheduleSettings, now: dt.datetime) -> dt.datetime:
    """最近一次「應該要執行」的時間（<= now）。"""
    tz = ZoneInfo(cfg.timezone)
    local = now.astimezone(tz)
    days_back = (local.weekday() - WEEKDAYS.index(cfg.day_of_week)) % 7
    fire = (local - dt.timedelta(days=days_back)).replace(hour=cfg.hour, minute=cfg.minute, second=0, microsecond=0)
    return fire - dt.timedelta(days=7) if fire > local else fire


class BotScheduler:
    def __init__(self, service: BotService) -> None:
        self.service = service
        self._scheduler = BackgroundScheduler()
        self._lock = threading.Lock()
        self.status = "尚未啟動"

    def start(self) -> None:
        self._scheduler.start()
        self.reload()
        self._maybe_catch_up()

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def reload(self) -> None:
        """設定改了之後呼叫，重新排程。"""
        with self._lock:
            if self._scheduler.get_job(JOB_ID):
                self._scheduler.remove_job(JOB_ID)
            try:
                cfg = load_settings(self.service.paths).schedule
            except ChurchBotError as exc:
                self.status = f"設定檔有誤，自動發送暫停：{exc.message}"
                log.error(self.status)
                return
            if not cfg.enabled:
                self.status = "自動發送已關閉"
                log.info(self.status)
                return
            trigger = CronTrigger(day_of_week=cfg.day_of_week, hour=cfg.hour, minute=cfg.minute,
                                  timezone=ZoneInfo(cfg.timezone))
            # misfire_grace_time：電腦卡住或睡眠，一小時內醒來還是會執行
            self._scheduler.add_job(self._run, trigger, id=JOB_ID, args=("schedule",), misfire_grace_time=3600,
                                    coalesce=True, max_instances=1, replace_existing=True)
            self.status = cfg.describe()
            log.info("自動發送：%s（下次：%s）", self.status, self.next_run_text())

    def next_run(self) -> dt.datetime | None:
        job = self._scheduler.get_job(JOB_ID)
        return getattr(job, "next_run_time", None) if job else None

    def next_run_text(self) -> str:
        nxt = self.next_run()
        if nxt is None:
            return "（沒有排程）"
        return nxt.strftime("%Y/%m/%d %H:%M") + "（" + "一二三四五六日"[nxt.weekday()].join(["週", ""]) + "）"

    def _run(self, trigger: str) -> None:
        try:
            self.service.run(trigger)
        except Exception:  # noqa: BLE001 - service.run 已經處理所有錯誤；這是最後一道防線
            log.exception("排程執行失敗")

    def _maybe_catch_up(self) -> None:
        try:
            cfg = load_settings(self.service.paths).schedule
        except ChurchBotError:
            return
        if not cfg.enabled:
            return
        now = dt.datetime.now(ZoneInfo(cfg.timezone))
        fire = previous_fire_time(cfg, now)
        if now - fire > dt.timedelta(hours=CATCHUP_HOURS):
            return
        last = self.service.history.last_run(("schedule", "catchup", "manual", "cli"))
        if last and dt.datetime.fromisoformat(last.started_at) >= fire:
            return
        log.warning("排程時間 %s 沒有執行到（電腦可能關機或睡眠），現在補發", fire.strftime("%m/%d %H:%M"))
        self._scheduler.add_job(self._run, args=("catchup",), id="catchup", replace_existing=True)
