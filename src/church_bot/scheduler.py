"""管理網頁（2-start、免費模式）內建的自動排程：後台開著時，每個牧區照自己的時間自動發。

每個牧區一個鬧鐘（星期幾、幾點、每幾週都各自設定）；Telegram 呼叫 ``cli.bat send`` 可以當備援，
兩邊都觸發也不會發兩次（防重複 + 跨程式的鎖，見 locking.py）。
* 每週固定時間執行一次那個牧區的 ``BotService.run("schedule")``；設定「每幾週發送一次」時，
  不符合的那幾週會直接跳過（判斷方式見 ``is_active_week``，跟哪一次啟動程式無關，重開機也不會錯亂）。
* 開機補發：如果排程時間電腦剛好關機 / 睡眠，之後 12 小時內打開程式會自動補發一次。
  已經送過的訊息不會重送（有防重複機制），所以補發很安全。
* 順便固定更新「本月 LINE 用量」的快照（見 _watch_quota、core/quota.py），主控台那一格才會自己變新。
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
from church_bot.ministries import Church
from church_bot.service import BotService

log = logging.getLogger(__name__)

JOB_PREFIX = "weekly-"  # 每個牧區一個：weekly-m1、weekly-m2…
QUOTA_JOB_ID = "quota-refresh"
QUOTA_EVERY_MINUTES = 5  # 多久檢查一次「該不該再問 LINE 用量」（真的會連網的頻率由 core/quota.py 的 FRESH 決定）
CATCHUP_HOURS = 12
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
    """開機補發用：「現在該不該補跑一次排程」；None = 該跑，否則回傳不跑的原因。

    自動發送要開著、這週是發送週、排程時間已經到了但沒超過 CATCHUP_HOURS、這一次還沒跑過（/提醒 不算）。
    """
    cfg = load_settings(service.paths).schedule
    if not cfg.enabled:
        return "自動發送已關閉（設定 → 什麼時候發）"
    now = now or dt.datetime.now(ZoneInfo(cfg.timezone))
    fire = previous_fire_time(cfg, now)
    if now - fire > dt.timedelta(hours=CATCHUP_HOURS):
        return f"還沒到發送時間（{cfg.describe()}，下次：{next_fire_time(cfg, now):%m/%d %H:%M}）"
    last = service.history.last_run(("schedule", "catchup", "manual", "cli"))
    if last and dt.datetime.fromisoformat(last.started_at) >= fire:
        return f"{fire:%m/%d %H:%M} 這一次已經發過了"
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


def due_on(cfg: ScheduleSettings, date: dt.date) -> bool:
    """這一天是不是這個牧區的發送日（自動發送開著、星期幾對、是發送週）。``cli.bat send`` 不指定牧區時用。"""
    return cfg.enabled and WEEKDAYS[date.weekday()] == cfg.day_of_week and is_active_week(cfg, date)


def _job_id(ministry_id: str) -> str:
    return f"{JOB_PREFIX}{ministry_id}"


class BotScheduler:
    """每個牧區一個鬧鐘：照它自己的「星期幾、幾點、每幾週」叫醒，只發那一個牧區。

    不是每隔幾分鐘去看「現在要不要發」，而是記住每個牧區下一次的時間，時間到了才醒來。
    牧區的設定改了（網頁存檔、LINE /設定）就呼叫 ``reload()`` 重排。
    """

    def __init__(self, church: Church) -> None:
        self.church = church
        self._scheduler = BackgroundScheduler()
        self._lock = threading.Lock()
        self.statuses: dict[str, str] = {}  # 牧區編號 → 「每星期四 20:00」「自動發送已關閉」…

    def start(self) -> None:
        self._scheduler.start()
        self.reload()
        self._maybe_catch_up()
        self._watch_quota()

    def shutdown(self) -> None:
        """關掉排程。正在發送的那一次會等它送完（重新啟動、Ctrl+C 都不會把發到一半的提醒切斷）。"""
        if self._scheduler.running:
            self._scheduler.shutdown(wait=True)

    def reload(self) -> None:
        """設定改了之後呼叫，所有牧區重新排程（新增、刪除牧區也一樣）。"""
        with self._lock:
            for job in self._scheduler.get_jobs():
                if job.id.startswith(JOB_PREFIX):
                    job.remove()
            statuses: dict[str, str] = {}
            for unit in self.church.units():
                try:
                    cfg = load_settings(unit.service.paths).schedule
                except ChurchBotError as exc:
                    statuses[unit.id] = f"設定檔有誤，自動發送暫停：{exc.message}"
                    log.error("「%s」%s", unit.name, statuses[unit.id])
                    continue
                if not cfg.enabled:
                    statuses[unit.id] = "自動發送已關閉"
                    continue
                # 每 N 週的判斷在 _run() 裡做；這裡的 CronTrigger 一律每週觸發一次，
                # 好處是重開機、改設定都不用管「上次是哪一週」，純粹用日期現算，不會累積誤差。
                trigger = CronTrigger(day_of_week=cfg.day_of_week, hour=cfg.hour, minute=cfg.minute,
                                      timezone=ZoneInfo(cfg.timezone))
                # misfire_grace_time：電腦卡住或睡眠，一小時內醒來還是會執行
                self._scheduler.add_job(self._run, trigger, id=_job_id(unit.id), args=(unit.id, "schedule"),
                                        misfire_grace_time=3600, coalesce=True, max_instances=1, replace_existing=True)
                statuses[unit.id] = cfg.describe()
                log.info("「%s」自動發送：%s（下次：%s）", unit.name, cfg.describe(), self.next_run_text(unit.id))
            self.statuses = statuses

    def status_for(self, ministry_id: str) -> str:
        return self.statuses.get(ministry_id, "自動發送已關閉")

    @property
    def status(self) -> str:
        """整個教會一句話：幾個牧區會自動發。"""
        on = [mid for mid in self.statuses if self._scheduler.get_job(_job_id(mid))]
        if not on:
            return "自動發送都關著"
        return f"{len(on)} 個牧區自動發送" if len(self.statuses) > 1 else self.statuses[on[0]]

    def next_run(self, ministry_id: str) -> dt.datetime | None:
        job = self._scheduler.get_job(_job_id(ministry_id))
        if job is None or job.next_run_time is None:
            return None
        try:
            cfg = load_settings(self.church.paths.for_ministry(ministry_id)).schedule
        except ChurchBotError:
            return job.next_run_time
        if cfg.every_n_weeks <= 1:
            return job.next_run_time
        return next_fire_time(cfg, dt.datetime.now(ZoneInfo(cfg.timezone)))

    def next_any(self) -> tuple[str, dt.datetime] | None:
        """所有牧區裡最快要發的那一個：(牧區編號, 時間)。"""
        upcoming = [(mid, when) for mid in self.statuses if (when := self.next_run(mid)) is not None]
        return min(upcoming, key=lambda pair: pair[1]) if upcoming else None

    def next_run_text(self, ministry_id: str) -> str:
        return format_when(self.next_run(ministry_id))

    def _run(self, ministry_id: str, trigger: str) -> None:
        try:
            unit = self.church.unit(ministry_id)
            if unit is None:
                log.warning("排程要發「%s」，但這個牧區已經不在了，跳過", ministry_id)
                return
            if trigger == "schedule":
                # 只有「自動排程」這個觸發方式才會被「每 N 週」跳過；
                # 補發（catchup，已經用 previous_fire_time 挑過正確的週）、手動、指令列一律照常執行。
                cfg = load_settings(unit.service.paths).schedule
                if cfg.every_n_weeks > 1 and not is_active_week(cfg, self._today(cfg.timezone)):
                    log.info("「%s」這週不是排定的發送週（設定為每 %d 週一次），跳過", unit.name, cfg.every_n_weeks)
                    return
            unit.service.run(trigger)
        except Exception:  # noqa: BLE001 - service.run 已經處理所有錯誤；這是最後一道防線
            log.exception("排程執行失敗（%s）", ministry_id)

    @staticmethod
    def _today(timezone: str) -> dt.date:
        return dt.datetime.now(ZoneInfo(timezone)).date()

    def _watch_quota(self) -> None:
        """本月 LINE 用量：開管理網頁時先問一次（所以重開就會更新），之後固定回來看該不該再問。

        「該不該」由快照自己決定（見 core/quota.py）：平常最多每 FRESH 問一次，
        剛發送完排在 5 分鐘後——那時 LINE 的統計才算得進這一次。用量是整個 LINE 帳號一份，所有牧區共用。
        """
        self._scheduler.add_job(self._refresh_quota, "interval", minutes=QUOTA_EVERY_MINUTES, id=QUOTA_JOB_ID,
                                next_run_time=dt.datetime.now(), coalesce=True, max_instances=1,
                                replace_existing=True)

    def _refresh_quota(self) -> None:
        try:
            self.church.refresh_quota()
        except Exception:  # noqa: BLE001 - 查用量失敗不該影響發送；refresh_quota 已經把原因記進快照
            log.exception("更新本月 LINE 用量失敗")

    def _maybe_catch_up(self) -> None:
        for unit in self.church.units():
            try:
                if scheduled_skip_reason(unit.service) is not None:
                    continue
            except ChurchBotError:
                continue
            log.warning("「%s」排程時間到了還沒執行（電腦可能關機或睡眠），現在補發", unit.name)
            self._scheduler.add_job(self._run, args=(unit.id, "catchup"), id=f"catchup-{unit.id}",
                                    replace_existing=True)


def format_when(when: dt.datetime | None) -> str:
    if when is None:
        return "（沒有排程）"
    return when.strftime("%Y/%m/%d %H:%M") + "（" + "一二三四五六日"[when.weekday()].join(["週", ""]) + "）"
