"""把狀態播報交給 Notifier_TB：這台電腦上專門負責「送東西到 Telegram」的那支 bot。

**這個檔案不會自己連 Telegram，也沒有任何輪詢。** 要講話的時候，往
``127.0.0.1`` 的 socket 丟一個封包就結束；平常完全不連網、不開執行緒、不跑迴圈。

為什麼走 Notifier_TB（``C:\\Users\\user\\Documents\\Notifier_TB``）而不是自己打 api.telegram.org：
這台電腦已經有一支在做這件事了，任何本地程式都可以推進去——``ISDStockProject`` 的 live monitor
（``post_event_socket``）、Notifier 自己的 ``test_socket.py`` 用的都是同一個封包格式::

    {"sig": <socket_secret>, "payload": <要送的文字>}   →  TCP 127.0.0.1:9999

好處是 token、收件人、角色、訊息追蹤全部是 Notifier 的事，這個專案一個金鑰都不用保管。

**設定**：預設去隔壁找 ``../Notifier_TB/config.json``（host、port、密鑰都讀那一份，所以密鑰
只存在一個地方）。Notifier 搬家就在 ``.env`` 填 ``NOTIFIER_TB_HOME``；完全不想接就填
``NOTIFIER_DISABLED=1``，播報會安靜地不做事，其他功能照常。

**Notifier 沒開著的時候**（它是「要用才開」、閒置 180 秒自己關的）：訊息不會丟掉，寫進
``data/notify_outbox.jsonl``，等下一次送得出去時一起補送（見 ``_spool`` / ``_flush``）。
補送只發生在「本來就要送東西」或「後台剛啟動」那一刻，不是背景輪詢。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import socket
import threading
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
RUNTIME_KEY = "service_runtime"  # 共用資料庫裡的那一格：後台現在開著沒、上一次是怎麼結束的


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


def find_notifier(paths: Paths) -> NotifierTarget | None:
    """讀出 Notifier_TB 的 socket 設定。找不到、或被關掉就回 None。"""
    if _env(paths, DISABLE_ENV):
        return None
    home = _env(paths, HOME_ENV)
    folder = Path(home).expanduser() if home else paths.church.root.parent / DEFAULT_DIR_NAME
    config_file = folder / "config.json"
    if not config_file.exists():
        return None
    try:
        config = json.loads(config_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("讀不懂 Notifier_TB 的 config.json（%s）：%s", config_file, exc)
        return None
    if not isinstance(config, dict):
        return None
    target = NotifierTarget(str(config.get("socket_host") or "127.0.0.1").strip(),
                            int(config.get("socket_port") or 9999),
                            str(config.get("socket_secret") or "").strip(),
                            str(config_file))
    return target if target.ready else None


class Notifier:
    """伺服器管理員的狀態播報。沒接上 Notifier_TB 就安靜地不做事，呼叫的人不用先檢查。"""

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
        return self.target is not None

    def describe(self) -> str:
        """給系統檢查那一頁看的一句話。"""
        target = self.target
        if target is None:
            return "還沒接上 Notifier_TB"
        waiting = self.pending()
        extra = f"；有 {waiting} 則還沒送出去（Notifier 沒開著時先存起來）" if waiting else ""
        return f"經由 Notifier_TB {target.host}:{target.port} 送到 Telegram{extra}"

    def pending(self) -> int:
        try:
            return sum(1 for line in self.outbox.read_text(encoding="utf-8").splitlines() if line.strip())
        except OSError:
            return 0

    # ------------------------------------------------------------------ 送

    def send(self, text: str) -> bool:
        """送一則給伺服器管理員。永遠不丟例外；回傳有沒有真的送出去。

        送不出去（Notifier 沒開著）不是錯誤，會先存進 outbox，之後補送。
        """
        target = self.target
        if target is None:
            return False
        with self._lock:
            delivered = self._post(target, text)
            if delivered:
                self._flush(target)  # 既然通了，順手把積欠的補送掉
            else:
                self._spool(text)
            return delivered

    def flush(self) -> int:
        """把積欠的補送出去，回傳補送了幾則。後台啟動時呼叫一次。"""
        target = self.target
        if target is None:
            return 0
        with self._lock:
            return self._flush(target)

    @staticmethod
    def _post(target: NotifierTarget, text: str) -> bool:
        """一個 TCP 連線、一個封包、關掉。格式跟 Notifier_TB 的 test_socket.py 一模一樣。"""
        packet = json.dumps({"sig": target.secret, "payload": text}, ensure_ascii=False)
        try:
            with socket.create_connection((target.host, target.port), timeout=TIMEOUT) as conn:
                conn.sendall(packet.encode("utf-8"))
        except ConnectionRefusedError:
            log.info("Notifier_TB 現在沒開著（%s），通知先存起來之後補送", target)
            return False
        except OSError as exc:
            log.warning("推給 Notifier_TB 失敗（%s）：%s", target, exc)
            return False
        return True

    # ------------------------------------------------------------------ 積欠的

    def _spool(self, text: str) -> None:
        entry = json.dumps({"at": _now_text(), "text": text}, ensure_ascii=False)
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

    def _flush(self, target: NotifierTarget) -> int:
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
            if not self._post(target, text):
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
