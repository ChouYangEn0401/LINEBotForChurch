"""把狀態播報交給別人送：ClawBot（優先）或 Notifier_TB（退路）。這個專案自己不保管 Telegram 金鑰。

**只給擁有者（伺服器管理員）的那幾則**（``Notifier.send``：發送彙總、後台開／關／當掉、``狀態 --telegram``、
webhook 的 ``restarting``／``stop_failed``）：2026-11-02 起**先打 ClawBot 的 ``POST /v1/notify``**
（``ClawBotLink.tell_owner``，只有機器擁有者的 token 能打、只送給擁有者），由 ClawBot 照它的分流表挑哪一支 bot
（擁有者 2026-11-02：「全部都改，按照現在的分流架構，決定應該要把訊息塞給誰」）。每一則標 ``stream``／``kind``：
都是 ``system``（緩衝 bot、會響）；開機／關機問候 ``system.hello``（不響）；乾淨的發送彙總、``狀態 --telegram``
帶 ``silent``；伺服器管理員登入碼 ``important``（一律主機 bot，見 login_codes.py）。ClawBot 找不到、連不上、
或它回錯誤，**退回 Notifier_TB**（下面那條），所以不會因此掉通知；log 一行寫這一則走了哪一條。
擁有者 2026-10-27 說後台的事只給擁有者——「別人不知道後台的事」：``/v1/notify`` 也只送給擁有者，
那個用意不變，只是換成 ClawBot 的 bot 送。

**webhook 的開關通知，兩條路差在收件人**（``webhook_window.tell_dev`` 照 ``webhook.dev_notice`` 挑）：

* ``ClawBotLink``（``dev_notice: clawbot``）：打 ClawBot 的本機入口 ``POST /v1/service/broadcast``
  ``{"grant": "line_bot"}``——送給**在 ClawBot 的服務 bot 上被開了「LINE 群組指令」權限的每一個人**，
  外加擁有者。webhook 這條臨時網址是大家共用的，誰開的、到幾點、什麼時候要關，他們也要知道
  （擁有者 2026-10-23：「之後傳遞訊息的也會回傳到這邊，那有權限的人就會收到」）。
  連不上 ClawBot（Host 沒開）就退回下面那條，所以不會因此掉通知。
* ``Notifier``（``dev_notice: notifier_tb``，預設）：只給伺服器管理員（2026-11-02 起也是先經 ClawBot 的
  ``/v1/notify``，再退回 Notifier_TB）。後台（church-bot）自己的事——臨時網址斷了 nssm 會重開、時間到卻停不掉
  服務——**永遠只給擁有者**（不管 dev_notice 是哪一個）：那幾則講的是服務、nssm、cloudflared 的結束代碼，
  不是給別人看的。

**這個檔案不會自己連 Telegram，也沒有任何輪詢。** 要講話的時候，打一次 ``127.0.0.1`` 的 ClawBot、
或往 ``127.0.0.1`` 的 socket 丟一個封包就結束；平常完全不連網、不開執行緒、不跑迴圈。

**退路：127.0.0.1:9999 的 socket（以前叫 Notifier_TB）。** 封包格式跟 ``ISDStockProject`` 的 live monitor
（``post_event_socket``）、Notifier 自己的 ``test_socket.py`` 一樣::

    {"sig": <密鑰>, "payload": <要送的文字>}   →  TCP 127.0.0.1:9999

Notifier_TB 已經併進 ClawBot 的 Host（2026-11-02，``feat/notifier-merge``）：**現在聽 9999 的是 Host**，
同一個埠、同一把密鑰（搬家腳本把 Notifier 的 ``socket_secret`` 搬成 ClawBot 的 ``signal_socket_secret``）。
所以這條退路只在「``/v1/notify`` 打不通、但 Host 的 socket 還收得到」或擁有者自己開著舊 Notifier 時有用。

**退路只試打一次、不叫醒任何程式。** 9999 沒人聽（Host 沒開）就**立刻**寫進 ``data/notify_outbox.jsonl``，
不開 Notifier_TB、不開任何子程序、不等（2026-11-02 起；以前會「幫它開」舊的 Notifier，那只會在 Host 沒開時把
一支退休的 bot 叫起來）。存起來的在下一次「本來就要送東西」或「後台剛啟動」（``flush``）時一起補送
（見 ``_spool`` / ``_flush``）——Host 開回來之後的下一則就會把積欠的帶走。不是背景輪詢。

**設定（密鑰從哪來）**：ClawBot 的 ``config.json`` 有 ``signal_socket_secret`` 就用它（``../MyFirstTelegramClawBot``，
搬家填 ``CLAWBOT_HOME``；Notifier_TB 資料夾之後可能被收掉）；沒有才讀 ``../Notifier_TB/config.json`` 的
``socket_secret``（搬家填 ``NOTIFIER_TB_HOME``）。host、port 照 Notifier 那份寫的，沒有就是 127.0.0.1:9999。
完全不想走這條退路就填 ``NOTIFIER_DISABLED=1``。密鑰只讀、不寫進 log。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import socket
import threading
import urllib.error
import urllib.request
from pathlib import Path

from church_bot.config import Paths, read_env_file
from church_bot.core.history import History

log = logging.getLogger(__name__)

HOME_ENV = "NOTIFIER_TB_HOME"
DISABLE_ENV = "NOTIFIER_DISABLED"
DEFAULT_DIR_NAME = "Notifier_TB"  # 跟本專案並排的那個資料夾
TIMEOUT = 5.0  # 跟 ISDStockProject 的 post_event_socket 一樣
OUTBOX_NAME = "notify_outbox.jsonl"
OUTBOX_LIMIT = 200  # 積太多就丟掉最舊的；這是通知，不是帳本
SIGNAL_HOST, SIGNAL_PORT = "127.0.0.1", 9999  # Notifier 那份沒寫時：ClawBot 的 Host 聽的地方
CLAWBOT_SECRET_KEY = "signal_socket_secret"    # ClawBot config.json 裡 9999 那把密鑰的鍵名
RUNTIME_KEY = "service_runtime"  # 共用資料庫裡的那一格：後台現在開著沒、上一次是怎麼結束的

CLAWBOT_HOME_ENV = "CLAWBOT_HOME"          # ClawBot 搬家了就在 .env 填這個
CLAWBOT_DISABLE_ENV = "CLAWBOT_DISABLED"   # 填 1＝不經 ClawBot（照舊只給伺服器管理員）
CLAWBOT_DIR_NAME = "MyFirstTelegramClawBot"  # 跟本專案並排的那個資料夾
CLAWBOT_GRANT = "line_bot"                 # ClawBot 服務台授權表上「LINE 群組指令」那一組叫什麼
CLAWBOT_TIMEOUT = 10.0
CLAWBOT_NOTIFY_TIMEOUT = 5.0               # 給擁有者的那一則：短一點，Host 卡住時還來得及退回 Notifier_TB
CLAWBOT_SOURCE = "LINE"                    # ClawBot 會把它寫在開頭「［LINE］…」
CLAWBOT_TEXT_LIMIT = 4000                  # /v1/notify 只收這麼長的純文字

# ClawBot 分流表（它的 src/clawbot/defaults/stream_routes.json）的流：這個專案只用 system
STREAM_SYSTEM = "system"
KIND_HELLO = "hello"                       # system.hello：開機／關機問候，緩衝 bot、不響


def _env(paths: Paths, key: str) -> str:
    """.env 的值（系統環境變數優先）。每次重讀，改完不用重開程式。"""
    return (os.environ.get(key) or read_env_file(paths.church.env_file).get(key, "")).strip()


class NotifierTarget:
    """Notifier_TB 在哪、怎麼連。找不到就 ``ready = False``。"""

    __slots__ = ("host", "port", "secret", "source")

    def __init__(self, host: str, port: int, secret: str, source: str) -> None:
        self.host, self.port, self.secret, self.source = host, port, secret, source

    @property
    def ready(self) -> bool:
        return bool(self.host and self.port and self.secret)

    def __str__(self) -> str:
        return f"{self.host}:{self.port}（{self.source}）"


def _read_config(config_file: Path | None, what: str) -> dict:
    """讀一份 config.json；沒有、讀不懂都回 {}（只寫檔名進 log，不印內容——裡面有密鑰）。"""
    if config_file is None or not config_file.is_file():
        return {}
    try:
        config = json.loads(config_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("讀不懂 %s 的 config.json（%s）：%s", what, config_file, type(exc).__name__)
        return {}
    return config if isinstance(config, dict) else {}


def find_notifier(paths: Paths) -> NotifierTarget | None:
    """讀出 9999 那條退路（ClawBot 的 Host／舊的 Notifier_TB 在聽的 socket）怎麼連。

    密鑰：ClawBot ``config.json`` 的 ``signal_socket_secret`` 優先，沒有才用 ``../Notifier_TB/config.json`` 的
    ``socket_secret``（兩邊是同一把，Notifier_TB 資料夾之後可能被收掉）。host、port 照 Notifier 那份，沒有就是
    127.0.0.1:9999。``source`` 寫讀了哪一份（不含密鑰）。哪一份都沒有密鑰、或被 ``NOTIFIER_DISABLED`` 關掉就回 None。
    """
    if _env(paths, DISABLE_ENV):
        return None
    home = _env(paths, HOME_ENV)
    folder = Path(home).expanduser() if home else paths.church.root.parent / DEFAULT_DIR_NAME
    notifier_file = folder / "config.json"
    notifier = _read_config(notifier_file, "Notifier_TB")
    clawbot_folder = _clawbot_folder(paths)
    clawbot_file = clawbot_folder / "config.json" if clawbot_folder is not None else None
    clawbot = _read_config(clawbot_file, "ClawBot")

    secret = str(clawbot.get(CLAWBOT_SECRET_KEY) or "").strip()
    if secret:
        secret_from = f"密鑰：{clawbot_file} 的 {CLAWBOT_SECRET_KEY}"
    else:
        secret = str(notifier.get("socket_secret") or "").strip()
        secret_from = f"密鑰：{notifier_file} 的 socket_secret"
    if not secret:
        return None
    try:
        port = int(notifier.get("socket_port") or SIGNAL_PORT)
    except (TypeError, ValueError):
        log.warning("Notifier_TB 的 config.json 的 socket_port 看不懂（%s），用 %d", notifier_file, SIGNAL_PORT)
        port = SIGNAL_PORT
    host = str(notifier.get("socket_host") or SIGNAL_HOST).strip()
    address_from = f"位址：{notifier_file}" if ("socket_host" in notifier or "socket_port" in notifier) else "位址：預設"
    target = NotifierTarget(host, port, secret, f"{secret_from}；{address_from}")
    return target if target.ready else None


class ClawBotLink:
    """經 ClawBot 的本機入口，送給「能開這條 webhook 的人」（＋擁有者）。

    ClawBot 從 2026-10-23 起是這台電腦上唯一往 Telegram 送東西的程式（別的程式不自己拿 bot token），
    所以這裡只打它的 HTTP 入口。**永遠不丟例外**；送不出去回 False，呼叫端自己決定要不要退回舊路。
    """

    __slots__ = ("url", "token", "source", "timeout", "_opener")

    def __init__(self, url: str, token: str, source: str = "", timeout: float = CLAWBOT_TIMEOUT) -> None:
        self.url, self.token, self.source, self.timeout = url.rstrip("/"), token, source, timeout
        # 127.0.0.1 不走 proxy：開了 VPN／proxy 軟體時 urllib 預設會把本機的請求也送去 proxy
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def __str__(self) -> str:
        return f"{self.url}（{self.source}）"

    def _post(self, path: str, body: dict) -> dict | None:
        """回傳 ClawBot 的回覆；連不上回 None（＝可以退回 Notifier_TB）。
        它收到了但說不行（400／403）回 {"error": …}：那是設定錯了，退回舊路也不會變對，所以照實回。"""
        request = urllib.request.Request(
            self.url + path, method="POST", data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with self._opener.open(request, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8") or "{}")
            except (ValueError, OSError):
                detail = {}
            log.warning("ClawBot 的 %s 回 %s：%s", path, exc.code, detail.get("message") or detail)
            return {"error": detail.get("error") or f"http_{exc.code}", "message": detail.get("message", "")}
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.info("連不上 ClawBot（%s）：%s", self, exc)
            return None

    def is_owner(self) -> bool:
        """這個 token 真的是機器擁有者的嗎（gateway.json 裡可能有別的、名字也叫 owner 的舊 token）。"""
        request = urllib.request.Request(self.url + "/v1/me",
                                         headers={"Authorization": f"Bearer {self.token}"})
        try:
            with self._opener.open(request, timeout=self.timeout) as resp:
                return bool(json.loads(resp.read().decode("utf-8") or "{}").get("owner"))
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def tell_grant_holders(self, text: str, grant: str = CLAWBOT_GRANT) -> bool | None:
        """送給開了這一組權限的人（＋擁有者）。True＝送出去了；False＝ClawBot 說不行；
        None＝連不上（呼叫端退回 Notifier_TB）。"""
        got = self._post("/v1/service/broadcast", {"grant": grant, "text": text, "source": "LINE"})
        if got is None:
            return None
        if got.get("error"):
            return False
        sent, failed = got.get("sent") or [], got.get("failed") or []
        log.info("經 ClawBot 通知有 %s 權限的人：%d 人收到、%d 人沒送到、擁有者 %s",
                 grant, len(sent), len(failed), got.get("owner"))
        return True

    def tell_owner(self, text: str, *, stream: str = STREAM_SYSTEM, kind: str = "", important: bool = False,
                   silent: bool = False) -> bool | None:
        """``POST /v1/notify``：只送給機器擁有者，ClawBot 照它的分流表（stream＋kind；important＝主機）挑哪一支 bot。
        True＝送出去了（主機還沒按 Start、ClawBot 先收著也算）；False＝ClawBot 說不行或沒送成；None＝連不上。
        內容是純文字、最多 4000 字（多的截掉）。"""
        if len(text) > CLAWBOT_TEXT_LIMIT:
            text = text[:CLAWBOT_TEXT_LIMIT - 1] + "…"
        body = {"text": text, "stream": stream, "kind": kind, "important": important, "silent": silent,
                "source": CLAWBOT_SOURCE}
        got = self._post("/v1/notify", body)
        if got is None:
            return None
        if got.get("error") or not got.get("ok"):
            return False
        log.info("經 ClawBot 送給擁有者（%s → %s bot%s）", f"{stream}.{kind}" if kind else stream,
                 got.get("bot", "?"), "；主機還沒按 Start，ClawBot 先收著" if got.get("held") else "")
        return True


def _clawbot_folder(paths: Paths) -> Path | None:
    """ClawBot 的資料夾（``CLAWBOT_HOME``，沒填就是專案隔壁）；被 ``CLAWBOT_DISABLED`` 關掉就回 None。不讀檔。"""
    if _env(paths, CLAWBOT_DISABLE_ENV):
        return None
    home = _env(paths, CLAWBOT_HOME_ENV)
    return Path(home).expanduser() if home else paths.church.root.parent / CLAWBOT_DIR_NAME


def _clawbot_store(paths: Paths) -> Path | None:
    """ClawBot 的 ``data/gateway.json`` 在哪；被 ``CLAWBOT_DISABLED`` 關掉就回 None。不讀檔、不連線。"""
    folder = _clawbot_folder(paths)
    return None if folder is None else folder / "data" / "gateway.json"


def clawbot_configured(paths: Paths) -> bool:
    """旁邊有沒有 ClawBot（有它的 gateway.json）。只看檔案在不在、不連線——系統檢查那一頁、登入碼的開關用。"""
    store_file = _clawbot_store(paths)
    return store_file is not None and store_file.is_file()


def find_clawbot(paths: Paths, *, timeout: float = CLAWBOT_TIMEOUT) -> ClawBotLink | None:
    """讀出 ClawBot 的本機入口（它的 ``data/gateway.json``：網址＋機器擁有者的 token）。
    被關掉、找不到、沒有擁有者的 token 就回 None。跟 ``find_notifier`` 一樣每次重讀，改完不用重開程式。"""
    store_file = _clawbot_store(paths)
    if store_file is None:
        return None
    try:
        store = json.loads(store_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.info("讀不到 ClawBot 的 gateway.json（%s）：%s", store_file, exc)
        return None
    if not isinstance(store, dict):
        return None
    url = str(store.get("url") or "http://127.0.0.1:8765")
    tokens = store.get("tokens") if isinstance(store.get("tokens"), dict) else {}
    # 名字叫 owner 的先試（可能有好幾個，只有一個是現在的機器擁有者），再試其他的；用 /v1/me 問它認誰
    ordered = sorted(tokens, key=lambda t: 0 if (tokens[t] or {}).get("name") == "owner" else 1)
    for token in ordered:
        link = ClawBotLink(url, str(token), str(store_file), timeout=timeout)
        if link.is_owner():
            return link
    log.info("ClawBot 的 gateway.json 裡沒有機器擁有者的 token（或 Host 沒開）：%s", store_file)
    return None


class Notifier:
    """伺服器管理員（＝機器擁有者）的狀態播報：先經 ClawBot 的 ``/v1/notify``，不行再經 Notifier_TB。
    兩個都沒接上就安靜地不做事，呼叫的人不用先檢查。"""

    def __init__(self, paths: Paths) -> None:
        self.paths = paths.church
        self._lock = threading.Lock()  # outbox 是一個檔案，多個牧區同時發送時要排隊

    @property
    def outbox(self) -> Path:
        return self.paths.data_dir / OUTBOX_NAME

    @property
    def target(self) -> NotifierTarget | None:
        return find_notifier(self.paths)

    @property
    def enabled(self) -> bool:
        return clawbot_configured(self.paths) or self.target is not None

    def describe(self) -> str:
        """給系統檢查那一頁看的一句話（不連線：ClawBot 只看它的 gateway.json 在不在）。"""
        target, clawbot = self.target, clawbot_configured(self.paths)
        if target is None and not clawbot:
            return "還沒接上 ClawBot 或 Notifier_TB"
        ways = (["先經 ClawBot"] if clawbot else []) + (
            [f"{'不行再' if clawbot else ''}試打一次 {target.host}:{target.port}（Host 的 socket／Notifier_TB）"]
            if target else [])
        waiting = self.pending()
        extra = f"；有 {waiting} 則還沒送出去（兩邊都沒開著時先存起來）" if waiting else ""
        return f"{'，'.join(ways)}送到 Telegram{extra}"

    def pending(self) -> int:
        try:
            return sum(1 for line in self.outbox.read_text(encoding="utf-8").splitlines() if line.strip())
        except OSError:
            return 0

    # ------------------------------------------------------------------ 送

    def send(self, text: str, *, stream: str = STREAM_SYSTEM, kind: str = "",
             silent: bool = False, important: bool = False, via_clawbot: bool = True) -> bool:
        """送一則給伺服器管理員。永遠不丟例外；回傳有沒有真的送出去。

        先經 ClawBot（``/v1/notify``；``stream``／``kind``／``silent``／``important`` 交給它的分流表）；
        ClawBot 找不到、連不上、回錯誤 → 退回 9999 的 socket，**只試打一次**（Host 開著就收得到）。
        ``via_clawbot=False``＝呼叫的人剛試過 ClawBot、確定連不上，直接試 9999。
        9999 也沒人聽 → 不叫醒任何程式、不等，**立刻**存進 outbox，之後補送；這不是錯誤。
        送得出去的那一次會順手把 outbox 積欠的補送掉（Host 開回來後的第一則就會帶走它們）。
        """
        link = find_clawbot(self.paths, timeout=CLAWBOT_NOTIFY_TIMEOUT) if via_clawbot else None
        target = self.target
        if link is None and target is None:
            log.info("通知沒送：ClawBot、Notifier_TB 都沒接上")
            return False
        opts = {"stream": stream, "kind": kind, "silent": silent, "important": important}
        with self._lock:
            way = self._deliver(link, target, text, opts)
            if way:
                log.info("通知給擁有者：經 %s 送出", way)
                self._flush(link, target)  # 既然通了，順手把積欠的補送掉
            else:
                log.info("通知給擁有者：ClawBot、9999 的 socket 都送不出去，先存起來之後補送（不叫醒任何程式）")
                self._spool(text, opts)
            return bool(way)

    def flush(self) -> int:
        """把積欠的補送出去，回傳補送了幾則。後台啟動時呼叫一次；還是不通就留著，不叫醒、不等。"""
        if not self._read_spool():
            return 0  # 沒有積欠的就不用去找 ClawBot、Notifier
        link = find_clawbot(self.paths, timeout=CLAWBOT_NOTIFY_TIMEOUT)
        target = self.target
        if link is None and target is None:
            return 0
        with self._lock:
            return self._flush(link, target)

    def _deliver(self, link: ClawBotLink | None, target: NotifierTarget | None, text: str, opts: dict) -> str:
        """送一則，回傳走了哪一條（"ClawBot"／"socket 9999"）；都不通回空字串。9999 只試一次。"""
        if link is not None:
            if link.tell_owner(text, **opts):
                return "ClawBot"
            if target is not None:
                log.info("ClawBot 沒送成，退回 9999 的 socket（試一次）")
        if target is not None and self._post(target, text):
            return f"socket {target.port}"
        return ""

    @staticmethod
    def _post(target: NotifierTarget, text: str) -> bool:
        """一個 TCP 連線、一個封包、關掉。格式跟 Notifier_TB 的 test_socket.py 一模一樣。
        沒人聽就回 False——**不會**去開 Notifier_TB 或任何程式。"""
        packet = json.dumps({"sig": target.secret, "payload": text}, ensure_ascii=False)
        try:
            with socket.create_connection((target.host, target.port), timeout=TIMEOUT) as conn:
                conn.sendall(packet.encode("utf-8"))
        except ConnectionRefusedError:
            log.info("%s:%d 沒人在聽（ClawBot 的 Host 沒開），不叫醒 Notifier_TB", target.host, target.port)
            return False
        except OSError as exc:
            log.warning("推給 %s:%d 失敗：%s", target.host, target.port, exc)
            return False
        return True

    # ------------------------------------------------------------------ 積欠的

    def _spool(self, text: str, opts: dict | None = None) -> None:
        entry = json.dumps({"at": _now_text(), "text": text, **(opts or {})}, ensure_ascii=False)
        try:
            self.outbox.parent.mkdir(parents=True, exist_ok=True)
            lines = self._read_spool()
            lines.append(entry)
            # 只留最後 OUTBOX_LIMIT 則：真的積到那麼多，最舊的也已經沒有參考價值了
            self.outbox.write_text("\n".join(lines[-OUTBOX_LIMIT:]) + "\n", encoding="utf-8")
        except OSError as exc:
            log.warning("存不下這則通知：%s", exc)

    def _read_spool(self) -> list[str]:
        try:
            return [line for line in self.outbox.read_text(encoding="utf-8").splitlines() if line.strip()]
        except OSError:
            return []

    def _flush(self, link: ClawBotLink | None, target: NotifierTarget | None) -> int:
        lines = self._read_spool()
        if not lines:
            return 0
        sent = 0
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                sent += 1  # 壞掉的那一行直接當作處理完，不要卡住後面的
                continue
            text = f"（補送・{entry.get('at', '')}）\n{entry.get('text', '')}"
            opts = {"stream": str(entry.get("stream") or STREAM_SYSTEM), "kind": str(entry.get("kind") or ""),
                    "silent": bool(entry.get("silent")), "important": bool(entry.get("important"))}
            if not self._deliver(link, target, text, opts):
                break  # 又不通了，剩下的留著下次再說
            sent += 1
        rest = lines[sent:]
        try:
            if rest:
                self.outbox.write_text("\n".join(rest) + "\n", encoding="utf-8")
            else:
                self.outbox.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("整理補送清單時失敗：%s", exc)
        if sent:
            log.info("補送了 %d 則之前沒送出去的通知", sent)
        return sent


# --------------------------------------------------------------------------- 後台開著沒

def _now_text() -> str:
    return dt.datetime.now().astimezone().strftime("%Y/%m/%d %H:%M:%S")


def record_start(history: History) -> dict:
    """記下「後台現在開著」，並回傳**上一次**那一筆（給啟動通知判斷有沒有正常關閉用）。"""
    before = history.get_state(RUNTIME_KEY)
    history.set_state(RUNTIME_KEY, {"running": True, "clean": False, "pid": os.getpid(),
                                    "started_at": _now_text()})
    return before


def record_stop(history: History, *, clean: bool, reason: str) -> None:
    """正常收工時記一筆。被強制關掉、斷電、當掉時這行根本跑不到——那正是下次啟動要講的事。"""
    state = history.get_state(RUNTIME_KEY)
    state.update({"running": False, "clean": clean, "reason": reason, "stopped_at": _now_text()})
    history.set_state(RUNTIME_KEY, state)


def previous_shutdown_line(before: dict) -> str:
    """上一次是怎麼結束的。正常結束（或根本沒有上一次）回空字串，不用講。"""
    if not before or before.get("clean"):
        return ""
    started = before.get("started_at") or "（不知道什麼時候）"
    return f"⚠️ 上一次沒有正常關閉（{started} 開的那一次）——可能當掉、被關掉或斷電。"
