"""自動排程：2-start 開著時在程式裡排程；或用 Windows 工作排程器在背景跑（見 wintask.py，不用開著視窗）。

兩種方式都用 ``scheduled_skip_reason`` 判斷「現在該不該發」，規則一樣。
* 每週固定時間執行一次 ``BotService.run("schedule")``；設定「每幾週發送一次」時，
  不符合的那幾週會直接跳過（判斷方式見 ``is_active_week``，跟哪一次啟動程式無關，重開機也不會錯亂）。
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
MAX_SCHEDULED_ATTEMPTS = 3  # 同一次排程失敗了最多重試到幾次（Windows 工作排程器每 15 分鐘檢查一次）
_SENDING_TRIGGERS = ("schedule", "catchup", "manual", "cli")  # 這些跑過才算「這一次發過了」；/提醒 不算
_WEEK_EPOCH = dt.date(2024, 1, 1)  # 固定基準點（星期一）；只用來算「第幾週」，不代表任何實際意義


def is_active_week(cfg: ScheduleSettings, date: dt.date) -> bool:
    """這一週要不要發送。只由「日期」計算，跟程式重開機、上次啟動時間完全無關。"""
    if cfg.every_n_weeks <= 1:
        return True
    week_index = (date - _WEEK_EPOCH).days // 7
    return week_index % cfg.every_n_weeks == 0


def previous_fire_time(cfg: ScheduleSettings, now: dt.datetime) -> dt.datetime:
    """最近一次「應該要執行」的時間（<= now），會跳過非發送週。"""
    tz = ZoneInfo(cfg.timezone)
    local = now.astimezone(tz)
    days_back = (local.weekday() - WEEKDAYS.index(cfg.day_of_week)) % 7
    fire = (local - dt.timedelta(days=days_back)).replace(hour=cfg.hour, minute=cfg.minute, second=0, microsecond=0)
    if fire > local:
        fire -= dt.timedelta(days=7)
    for _ in range(cfg.every_n_weeks):
        if is_active_week(cfg, fire.date()):
            return fire
        fire -= dt.timedelta(days=7)
    return fire  # 理論上一定會在迴圈裡回傳（每 N 週裡一定有一週符合），這行只是保險


def scheduled_skip_reason(service: BotService, now: dt.datetime | None = None) -> str | None:
    """「現在該不該跑一次排程發送」；None = 該跑，否則回傳不跑的原因。

    規則跟程式內建的排程（含開機補發）一樣：自動發送要開著、這週是發送週、排程時間已經到了但沒超過
    CATCHUP_HOURS、這一次還沒跑過。所以可以隨時、重複呼叫（Windows 工作排程器每 15 分鐘叫一次、
    Telegram 叫、同時開著 2-start 也一樣），真正會發的只有「到時間、還沒發」的那一次。
    """
    cfg = load_settings(service.paths).schedule
    if not cfg.enabled:
        return "自動發送已關閉（設定 → ② 自動發送）"
    now = now or dt.datetime.now(ZoneInfo(cfg.timezone))
    fire = previous_fire_time(cfg, now)
    if now - fire > dt.timedelta(hours=CATCHUP_HOURS):
        return f"還沒到發送時間（{cfg.describe()}，下次：{next_fire_time(cfg, now):%m/%d %H:%M}）"
    runs = [r for r in service.history.recent_runs(limit=50) if r.trigger in _SENDING_TRIGGERS
            and dt.datetime.fromisoformat(r.started_at) >= fire]
    if any(r.status != "error" for r in runs):
        return f"{fire:%m/%d %H:%M} 這一次已經發過了"
    # 失敗的（例如電腦剛睡醒、網路還沒連上就去讀服事表）下次檢查再試；最多試幾次，免得一直通知管理員
    if len(runs) >= MAX_SCHEDULED_ATTEMPTS:
        return f"{fire:%m/%d %H:%M} 這一次試了 {len(runs)} 次都失敗，先不試了（管理員已收到通知）"
    return None


def next_fire_time(cfg: ScheduleSettings, now: dt.datetime) -> dt.datetime:
    """下一次「真的會執行」的時間（> now），會跳過非發送週。"""
    tz = ZoneInfo(cfg.timezone)
    local = now.astimezone(tz)
    days_fwd = (WEEKDAYS.index(cfg.day_of_week) - local.weekday()) % 7
    fire = (local + dt.timedelta(days=days_fwd)).replace(hour=cfg.hour, minute=cfg.minute, second=0, microsecond=0)
    if fire <= local:
        fire += dt.timedelta(days=7)
    for _ in range(cfg.every_n_weeks):
        if is_active_week(cfg, fire.date()):
            return fire
        fire += dt.timedelta(days=7)
    return fire


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
            # 每 N 週的判斷在 _run() 裡做；這裡的 CronTrigger 一律每週觸發一次，
            # 好處是重開機、改設定都不用管「上次是哪一週」，純粹用日期現算，不會累積誤差。
            trigger = CronTrigger(day_of_week=cfg.day_of_week, hour=cfg.hour, minute=cfg.minute,
                                  timezone=ZoneInfo(cfg.timezone))
            # misfire_grace_time：電腦卡住或睡眠，一小時內醒來還是會執行
            self._scheduler.add_job(self._run, trigger, id=JOB_ID, args=("schedule",), misfire_grace_time=3600,
                                    coalesce=True, max_instances=1, replace_existing=True)
            self.status = cfg.describe()
            log.info("自動發送：%s（下次：%s）", self.status, self.next_run_text())

    def next_run(self) -> dt.datetime | None:
        job = self._scheduler.get_job(JOB_ID)
        if job is None or job.next_run_time is None:
            return None
        try:
            cfg = load_settings(self.service.paths).schedule
        except ChurchBotError:
            return job.next_run_time
        if cfg.every_n_weeks <= 1:
            return job.next_run_time
        return next_fire_time(cfg, dt.datetime.now(ZoneInfo(cfg.timezone)))

    def next_run_text(self) -> str:
        nxt = self.next_run()
        if nxt is None:
            return "（沒有排程）"
        return nxt.strftime("%Y/%m/%d %H:%M") + "（" + "一二三四五六日"[nxt.weekday()].join(["週", ""]) + "）"

    def _run(self, trigger: str) -> None:
        try:
            if trigger == "schedule":
                # 只有「自動排程」這個觸發方式才會被「每 N 週」跳過；
                # 補發（catchup，已經用 previous_fire_time 挑過正確的週）、手動、指令列一律照常執行。
                cfg = load_settings(self.service.paths).schedule
                if cfg.every_n_weeks > 1 and not is_active_week(cfg, self._today(cfg.timezone)):
                    log.info("這週不是排定的發送週（設定為每 %d 週一次），跳過", cfg.every_n_weeks)
                    return
            self.service.run(trigger)
        except Exception:  # noqa: BLE001 - service.run 已經處理所有錯誤；這是最後一道防線
            log.exception("排程執行失敗")

    @staticmethod
    def _today(timezone: str) -> dt.date:
        return dt.datetime.now(ZoneInfo(timezone)).date()

    def _maybe_catch_up(self) -> None:
        try:
            if scheduled_skip_reason(self.service) is not None:
                return
        except ChurchBotError:
            return
        log.warning("排程時間到了還沒執行（電腦可能關機或睡眠），現在補發")
        self._scheduler.add_job(self._run, args=("catchup",), id="catchup", replace_existing=True)
