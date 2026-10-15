"""LINE 指令那條臨時網址（3-open-webhook／church-bot-webhook 服務）開多久、開關時通知誰。

擁有者 2026-10-15：webhook 要用的時候才開（Telegram 叫 ClawBot 開，或小圖示「服務：啟動」），
開幾個小時自己關；關之前先跟管理員講一聲「後台將停止訪問，需要的話請聯絡伺服器管理員」，
每週提醒照常（那是後台 church-bot 在發，不靠這條網址）。後台本身一直開著，壞了 nssm 會重開。

**時間記在哪**：``data/run/webhook_window.json``（一個小檔案）::

    {"until": "2026-10-15T18:30:00+08:00" | null, "hours": 3, "by": "telegram", "opened_at": ..., "warned": false}

* 臨時網址一開起來（``begin``）就寫下「到幾點關」：同一個時段內 nssm 重開它，時間照舊，不會重新算 3 小時。
* ``cli.bat webhook on --hours 2``：還沒開就先留一張「要開幾小時」的單子再啟動；已經開著就是延長（從現在起算）。
* 開著的那個程式裡有一條執行緒（``watch``）看這個檔案：最多每 60 秒讀一次（讀一個小檔案，不連網），
  快到了先預告（``warn_minutes``），時間到就自己關——服務的話是請 Windows 停掉自己這個服務，
  不然 nssm 會以為它當掉了、10 秒後又開起來。
* 關掉的時候（不管是時間到、Telegram、小圖示、有人停服務）記下原因，小圖示看得到是怎麼關的。

**通知誰**（``config/church.yaml`` 的 ``webhook:``，見 config.WebhookSettings）：

* 開發者（伺服器管理員）：預設經 Notifier_TB 傳 Telegram，不佔 LINE 額度。開源出去別人不一定用 Telegram，
  所以也可以換成「執行一個程式」（``dev_notice: command`` + ``dev_command``），事件內容當參數和環境變數給它，
  或乾脆關掉。
* 牧區管理員：用 LINE 私訊（每人每次 1 則），**預設關**——LINE 額度現在不夠用（擁有者 2026-10-15）。
  通知裡「請聯絡○○」的名字來自同工名單勾了「伺服器管理員」的人。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from church_bot.config import Paths, WebhookSettings, load_settings, load_webhook_settings
from church_bot.errors import ChurchBotError

if TYPE_CHECKING:
    from church_bot.ministries import Church

log = logging.getLogger(__name__)

FILE_NAME = "webhook_window.json"
REQUEST_TTL = 600        # 「要開幾小時」的單子多久內有效（秒）：服務起來通常不到一分鐘
CHECK_EVERY = 60.0       # 開著的時候最多隔多久重讀一次（延長、提早關都靠這個生效）
COMMAND_TIMEOUT = 30     # dev_command 最多跑多久

OPENED, EXTENDED, CLOSING_SOON, CLOSED = "opened", "extended", "closing_soon", "closed"
EVENTS = (OPENED, EXTENDED, CLOSING_SOON, CLOSED)

REASON_TIME = "時間到"


def _now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def _parse(text: object) -> dt.datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        value = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return value if value.tzinfo else value.astimezone()


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def clock(value: dt.datetime | None) -> str:
    """給人看的時間：今天的只寫 15:30，不是今天的寫 10/16 15:30。"""
    if value is None:
        return "不會自動關"
    local = value.astimezone()
    if local.date() == _now().date():
        return local.strftime("%H:%M")
    return f"{local.month}/{local.day} {local.strftime('%H:%M')}"


def hours_text(hours: float) -> str:
    return f"{hours:g} 小時"


# --------------------------------------------------------------------------- 檔案


def window_file(paths: Paths) -> Path:
    return paths.church.root / "data" / "run" / FILE_NAME


@dataclass
class Window:
    until: dt.datetime | None = None   # None = 不自動關
    hours: float = 0
    by: str = ""
    opened_at: dt.datetime | None = None
    warned: bool = False
    requested_at: dt.datetime | None = None  # 還沒開，只是一張「要開幾小時」的單子
    closed_at: dt.datetime | None = None
    reason: str = ""                          # 怎麼關的（關掉之後才有）；關之前是「誰要它關」

    @property
    def is_open(self) -> bool:
        return self.opened_at is not None and self.closed_at is None

    @property
    def is_request(self) -> bool:
        return self.requested_at is not None and self.opened_at is None

    def remaining(self, now: dt.datetime | None = None) -> float | None:
        """還剩幾秒；不自動關回 None。"""
        if self.until is None:
            return None
        return (self.until - (now or _now())).total_seconds()

    def to_dict(self) -> dict:
        return {"until": _iso(self.until), "hours": self.hours, "by": self.by, "opened_at": _iso(self.opened_at),
                "warned": self.warned, "requested_at": _iso(self.requested_at), "closed_at": _iso(self.closed_at),
                "reason": self.reason}

    @classmethod
    def from_dict(cls, data: dict) -> "Window":
        try:
            hours = float(data.get("hours") or 0)
        except (TypeError, ValueError):
            hours = 0
        return cls(until=_parse(data.get("until")), hours=hours, by=str(data.get("by") or ""),
                   opened_at=_parse(data.get("opened_at")), warned=bool(data.get("warned")),
                   requested_at=_parse(data.get("requested_at")), closed_at=_parse(data.get("closed_at")),
                   reason=str(data.get("reason") or ""))


_FILE_LOCK = threading.Lock()


def read(paths: Paths) -> Window | None:
    try:
        data = json.loads(window_file(paths).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return Window.from_dict(data) if isinstance(data, dict) else None


def write(paths: Paths, window: Window) -> None:
    """寫不進去只記 log：這個檔案壞掉，臨時網址照樣要開得起來（頂多不會自己關）。"""
    target = window_file(paths)
    with _FILE_LOCK:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(window.to_dict(), ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, target)
        except OSError as exc:
            log.warning("寫不下 %s：%s", target, exc)


def _until(hours: float, now: dt.datetime) -> dt.datetime | None:
    return now + dt.timedelta(hours=hours) if hours > 0 else None


def request(paths: Paths, hours: float, by: str, *, now: dt.datetime | None = None) -> Window:
    """還沒開：留一張「開起來之後開幾小時」的單子（``webhook on`` 先寫這個，再去啟動服務）。"""
    now = now or _now()
    window = Window(hours=hours, by=by, requested_at=now)
    write(paths, window)
    return window


def extend(paths: Paths, hours: float, by: str, *, now: dt.datetime | None = None) -> Window:
    """已經開著：從現在起再開 ``hours`` 小時（0 = 不自動關）。可以比原本長，也可以比原本短。"""
    now = now or _now()
    window = read(paths) or Window(opened_at=now)
    if not window.is_open:
        window = Window(opened_at=now)
    window.until, window.hours, window.by, window.warned = _until(hours, now), hours, by, False
    write(paths, window)
    return window


def begin(paths: Paths, settings: WebhookSettings, *, now: dt.datetime | None = None) -> Window:
    """臨時網址剛開起來：決定開到幾點。

    * 同一個時段還沒到（例如 nssm 剛把它重開）→ 照舊，不重新算；
    * 有 ``webhook on`` 留下的單子（10 分鐘內）→ 照單子上的小時數；
    * 其他（小圖示「服務：啟動」、雙擊 3-open-webhook、服務管理員按啟動）→ 設定的 ``hours``（預設 3）。
    """
    now = now or _now()
    old = read(paths)
    if old and old.is_open and (old.until is None or old.until > now):
        return old
    if old and old.is_request and (now - old.requested_at).total_seconds() <= REQUEST_TTL:
        hours, by = old.hours, old.by
    else:
        hours, by = settings.hours, ("服務" if os.environ.get("CHURCH_BOT_SERVICE") else "手動")
    window = Window(until=_until(hours, now), hours=hours, by=by, opened_at=now)
    write(paths, window)
    return window


def mark_closing(paths: Paths, reason: str) -> None:
    """要關它的人先寫下為什麼（``webhook off``）：關的那一刻，開著的程式就知道該怎麼說。"""
    window = read(paths)
    if window and window.is_open:
        window.reason = reason
        write(paths, window)


def finish(paths: Paths, reason: str, *, now: dt.datetime | None = None) -> Window | None:
    """關掉了：記下什麼時候、為什麼。回傳關掉的那一個時段（給通知用）；本來就沒開回 None。"""
    window = read(paths)
    if not window or not window.is_open:
        return None
    window.closed_at, window.reason = now or _now(), window.reason or reason
    write(paths, window)
    return window


# --------------------------------------------------------------------------- 通知


def server_admin_names(church: "Church") -> list[str]:
    """同工名單裡勾了「伺服器管理員」的人（每個牧區都看，同一個名字只算一次）。"""
    names: list[str] = []
    for unit in church.units():
        for member in unit.members():
            if member.server_admin and member.name not in names:
                names.append(member.name)
    return names


def contact_text(names: list[str]) -> str:
    return f"伺服器管理員（{'、'.join(names)}）" if names else "伺服器管理員"


def dev_text(event: str, window: Window | None, *, url: str = "", reason: str = "", ok: bool = True,
             now: dt.datetime | None = None) -> str:
    """給開發者（伺服器管理員）看的那一則。"""
    until = window.until if window else None
    how = "延長：cli.bat webhook on --hours 小時數；現在關：cli.bat webhook off"
    if event == OPENED:
        head = "🟢 LINE webhook 開好了" if ok else "🟡 LINE webhook 開了，但登記到 LINE 沒有完全成功"
        lines = [f"{head}：{url}" if url else head,
                 f"到 {clock(until)} 自動關（{hours_text(window.hours)}）" if until else "不會自動關（hours = 0）", how]
    elif event == EXTENDED:
        lines = [f"⏩ LINE webhook 改成到 {clock(until)} 自動關" if until else "⏩ LINE webhook 改成不自動關",
                 f"（{window.by} 改的）" if window and window.by else "", how]
    elif event == CLOSING_SOON:
        left = max(int(round((window.remaining(now) or 0) / 60)), 1) if window else 0
        lines = [f"⏰ LINE webhook 再 {left} 分鐘（{clock(until)}）自動關", how]
    else:
        lines = [f"⏹ LINE webhook 已關閉（{reason or '有人關掉'}）",
                 "群組指令不會回、外面也進不了管理網頁；每週提醒照常（後台還開著）。"]
    return "\n".join(line for line in lines if line)


def admin_text(event: str, window: Window | None, names: list[str], *, now: dt.datetime | None = None) -> str:
    """給牧區管理員看的那一則（LINE）。用詞照擁有者 2026-10-15：後台將停止訪問、需要的話請聯絡、提醒照常。"""
    contact = contact_text(names)
    until = window.until if window else None
    if event == OPENED:
        tail = f"，到 {clock(until)} 會自動關閉" if until else ""
        return (f"✅ LINE 群組指令（/提醒、/我的名字…）和從外面進管理網頁的網址已經開了{tail}。\n"
                "管理員在 LINE 打「/服務網址」可以拿到管理網頁的網址。")
    if event == CLOSING_SOON:
        left = max(int(round((window.remaining(now) or 0) / 60)), 1) if window else 0
        return (f"⏰ 再 {left} 分鐘（{clock(until)}），LINE 群組指令和從外面進管理網頁的網址就會關閉，後台將停止訪問。\n"
                f"需要繼續使用的話，請聯絡{contact}。\n"
                "每週的服事提醒照常會自動發送，不受影響。")
    return ("⏹ LINE 群組指令和從外面進管理網頁的網址已經關閉，後台停止訪問。\n"
            f"需要的話，請聯絡{contact}。\n"
            "每週的服事提醒照常會自動發送，不受影響。")


def event_payload(event: str, text: str, window: Window | None, *, url: str = "", reason: str = "",
                  now: dt.datetime | None = None) -> dict:
    """丟給 dev_command 的整份事件（環境變數 CHURCH_BOT_EVENT_JSON）。欄位以後只加不改。"""
    return {"source": "church-bot", "event": event, "text": text, "at": _iso(now or _now()),
            "until": _iso(window.until) if window else None, "hours": window.hours if window else None,
            "by": window.by if window else "", "url": url, "reason": reason}


def run_command(argv: list[str], payload: dict) -> bool:
    """dev_notice = command：照 dev_command 執行一次。不經過命令列（shell），{event} 這些只是換字。"""
    if not argv:
        log.warning("webhook.dev_notice 是 command，但 dev_command 是空的，這次沒通知")
        return False
    fields = {key: "" if payload.get(key) is None else str(payload[key]) for key in ("event", "text", "until", "url")}
    command = [part.replace("{event}", fields["event"]).replace("{text}", fields["text"])
               .replace("{until}", fields["until"]).replace("{url}", fields["url"]) for part in argv]
    env = dict(os.environ, CHURCH_BOT_EVENT=str(payload.get("event", "")),
               CHURCH_BOT_EVENT_JSON=json.dumps(payload, ensure_ascii=False))
    try:
        done = subprocess.run(command, env=env, capture_output=True, timeout=COMMAND_TIMEOUT,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("dev_command 執行失敗：%s", exc)
        return False
    if done.returncode != 0:
        log.warning("dev_command 結束代碼 %s", done.returncode)
    return done.returncode == 0


def tell_dev(paths: Paths, settings: WebhookSettings, payload: dict, *, wait: float | None = None) -> bool:
    """通知開發者。永遠不丟例外。"""
    try:
        if settings.dev_notice == "off":
            return False
        if settings.dev_notice == "command":
            return run_command(settings.dev_command, payload)
        from church_bot.core import notify

        notifier = notify.Notifier(paths)
        return notifier.send(payload["text"]) if wait is None else notifier.send(payload["text"], wait=wait)
    except Exception:  # noqa: BLE001 - 通知失敗不能害臨時網址開不起來／關不掉
        log.exception("通知開發者失敗（%s）", payload.get("event"))
        return False


def tell_admins(church: "Church", text: str) -> int:
    """LINE 私訊每一位牧區管理員（同工名單勾了「管理員」、對應好 LINE 帳號的人；同一個人只傳一次）。
    測試模式的牧區照它自己的方式（寫進 outbox.log，不會真的送）。回傳傳了幾位。"""
    from church_bot.messengers import build_messenger
    from church_bot.models import OutgoingMessage

    sent: set[str] = set()
    for unit in church.units():
        targets = [m.line_user_id for m in unit.members() if m.admin and m.line_user_id and m.line_user_id not in sent]
        if not targets:
            continue
        try:
            settings = load_settings(unit.service.paths)
            messenger = build_messenger(settings, unit.service.paths)
        except ChurchBotError as exc:
            log.warning("「%s」沒辦法用 LINE 通知管理員：%s", unit.name, exc.message)
            continue
        try:
            for target in targets:
                try:
                    messenger.send(target, OutgoingMessage(text=text))
                    sent.add(target)
                except ChurchBotError as exc:
                    log.warning("LINE 通知管理員失敗（%s）：%s", unit.name, exc.message)
        finally:
            messenger.close()
    if sent:
        log.info("webhook 開關：已用 LINE 通知 %d 位管理員", len(sent))
    return len(sent)


Announce = Callable[..., None]


def announce(paths: Paths, event: str, window: Window | None, *, url: str = "", reason: str = "", ok: bool = True,
             settings: WebhookSettings | None = None, church: "Church | None" = None, wait: float | None = None,
             now: dt.datetime | None = None) -> None:
    """一個事件：開發者一定（照 dev_notice），牧區管理員看開關（line_admins_on_open／on_close）。"""
    settings = settings or load_webhook_settings(paths)
    text = dev_text(event, window, url=url, reason=reason, ok=ok, now=now)
    tell_dev(paths, settings, event_payload(event, text, window, url=url, reason=reason, now=now), wait=wait)
    to_admins = (event == OPENED and settings.line_admins_on_open) or \
                (event in (CLOSING_SOON, CLOSED) and settings.line_admins_on_close)
    if not to_admins:
        return
    try:
        from church_bot.ministries import Church

        church = church or Church(paths.church)
        tell_admins(church, admin_text(event, window, server_admin_names(church), now=now))
    except Exception:  # noqa: BLE001
        log.exception("用 LINE 通知管理員失敗（%s）", event)


# --------------------------------------------------------------------------- 開著的時候看時間


def watch(paths: Paths, close: Callable[[], None], stop: threading.Event, *,
          announce_fn: Announce | None = None, now_fn: Callable[[], dt.datetime] | None = None,
          check_every: float = CHECK_EVERY) -> None:
    """開著的臨時網址裡的一條執行緒：快到了預告、時間到就呼叫 ``close()``，然後結束。

    ``stop`` 被設起來（臨時網址因為別的原因關了）就直接結束。延長、縮短都是改檔案，這裡下一輪就看到。
    預告只對「牧區管理員要不要收」有差：預告過了，關的那一刻就不再傳 LINE 給他們（省一則）。
    """
    announce_fn = announce_fn or announce
    now_fn = now_fn or _now
    warned_for: dt.datetime | None = None  # 這一個「到幾點」已經預告過（檔案寫不進去也不會一直重複預告）
    while not stop.is_set():
        window = read(paths)
        if window is None or not window.is_open:
            return  # 有人已經把它記成關掉了
        settings = load_webhook_settings(paths)
        now = now_fn()
        left = window.remaining(now)
        if left is None:
            stop.wait(check_every)
            continue
        warned = window.warned or (warned_for is not None and warned_for == window.until)
        warn = settings.warn_minutes * 60
        if left <= 0:
            closed = finish(paths, REASON_TIME, now=now)
            # 預告過就不用再傳一次 LINE 給牧區管理員（設定開著的話）；開發者照樣收到「已關閉」
            quiet = settings.model_copy(update={"line_admins_on_close": False}) if warned else settings
            announce_fn(paths, CLOSED, closed or window, reason=REASON_TIME, settings=quiet, now=now)
            close()
            return
        if warn and left <= warn and not warned:
            announce_fn(paths, CLOSING_SOON, window, settings=settings, now=now)
            warned_for = window.until
            latest = read(paths)
            if latest and latest.until == window.until and latest.is_open:  # 這中間沒人延長才記「預告過了」
                latest.warned = True
                write(paths, latest)
            continue
        next_mark = left - warn if (warn and not warned and left > warn) else left
        stop.wait(max(min(next_mark, check_every), 0.05))


def start_watch(paths: Paths, close: Callable[[], None]) -> threading.Event:
    """在背景開 ``watch``；回傳的 event 設起來它就結束。"""
    stop = threading.Event()
    threading.Thread(target=watch, args=(paths, close, stop), name="webhook-window", daemon=True).start()
    return stop


def describe(window: Window | None, *, now: dt.datetime | None = None) -> str:
    """一句話：到幾點關、還剩多久。"""
    if window is None or not window.is_open:
        if window and window.closed_at:
            return f"沒開（{clock(window.closed_at)} 關的：{window.reason or '有人關掉'}）"
        return "沒開"
    if window.until is None:
        return "開著，不會自動關"
    left = max(int((window.remaining(now) or 0) // 60), 0)
    return f"開著，到 {clock(window.until)} 自動關（還有 {left // 60} 小時 {left % 60} 分）" if left >= 60 else \
        f"開著，到 {clock(window.until)} 自動關（還有 {left} 分）"


def wait_for(predicate: Callable[[], bool], seconds: float, step: float = 1.0) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(step)
