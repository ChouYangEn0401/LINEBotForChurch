"""狀態播報（core/notify.py，走 Notifier_TB）與「各牧區狀況」（status.py、cli.bat 狀態）。"""

from __future__ import annotations

import datetime as dt
import json
import socket
import threading
import time

import pytest

from church_bot import status as status_mod
from church_bot.cli import main
from church_bot.config import Settings, save_settings
from church_bot.core import notify
from church_bot.core.history import History
from church_bot.ministries import Church
from church_bot.models import Target
from church_bot.tables import TargetTable
from tests.conftest import gid, write

ROSTER = """日期,講員,司琴
2026/9/13,王牧師,小明
2026/12/27,王牧師,美華
"""


@pytest.fixture
def ministry(paths):
    write(paths.config_dir / "roster.csv", ROSTER)
    settings = Settings()
    settings.source.kind = "csv"
    settings.source.csv_path = "roster.csv"
    settings.messenger.kind = "console"
    settings.schedule.day_of_week = "thu"
    settings.schedule.time = "20:00"
    save_settings(paths, settings)
    TargetTable(paths.targets_file).save([Target("同工群", gid())])
    return paths


# --------------------------------------------------------------------------- Notifier_TB


class FakeNotifier:
    """假的 Notifier_TB：真的開一個 TCP listener 收封包。

    刻意用真 socket 而不是 monkeypatch——這樣封包格式、連線行為都真的被測到，
    跟 Notifier_TB 的 test_socket.py 收到的會是同一個東西。
    """

    def __init__(self) -> None:
        self.received: list[dict] = []
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(8)
        self.port = self._srv.getsockname()[1]
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            with conn:
                chunks = []
                while chunk := conn.recv(65536):
                    chunks.append(chunk)
            try:
                self.received.append(json.loads(b"".join(chunks).decode("utf-8")))
            except ValueError:
                pass

    def close(self) -> None:
        self._stop = True
        try:  # Windows 上關 socket 不一定叫得醒卡在 accept() 的執行緒，自己連一下把它推醒
            socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
        except OSError:
            pass
        self._srv.close()
        self._thread.join(timeout=2)

    def wait(self, count: int, timeout: float = 5.0) -> list[dict]:
        """等到收滿 count 個封包。送出去和收下來是兩個執行緒，不等會抓到空的。"""
        deadline = time.monotonic() + timeout
        while len(self.received) < count and time.monotonic() < deadline:
            time.sleep(0.01)
        return self.received

    @property
    def payloads(self) -> list[str]:
        return [item.get("payload", "") for item in self.received]


def dead_port() -> int:
    """綁一個再放掉：這個埠現在保證沒有人在聽（模擬 Notifier_TB 沒開著）。"""
    spare = socket.socket()
    spare.bind(("127.0.0.1", 0))
    port = spare.getsockname()[1]
    spare.close()
    return port


def write_notifier_config(paths, *, port: int, secret: str = "probe-secret", folder: str = "Notifier_TB"):
    """在專案隔壁擺一個長得像 Notifier_TB 的資料夾（只要 config.json 就夠）。"""
    home = paths.church.root.parent / folder
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(json.dumps(
        {"bot_token": "notifier-keeps-this", "socket_host": "127.0.0.1",
         "socket_port": port, "socket_secret": secret}), encoding="utf-8")
    return home


@pytest.fixture
def fake_notifier():
    server = FakeNotifier()
    yield server
    server.close()


def test_notifier_is_quiet_when_there_is_no_notifier_tb(paths):
    notifier = notify.Notifier(paths)
    assert not notifier.enabled
    assert notifier.send("哈囉") is False
    assert notifier.pending() == 0  # 根本沒接上就不用存，也不會有積欠


def test_notifier_pushes_the_house_packet_format(paths, fake_notifier):
    """封包要跟 Notifier_TB 的 test_socket.py、ISDStockProject 的 post_event_socket 一樣。"""
    write_notifier_config(paths, port=fake_notifier.port, secret="s3cret")
    notifier = notify.Notifier(paths)
    assert notifier.enabled and notifier.send("後台開起來了") is True
    assert fake_notifier.wait(1) == [{"sig": "s3cret", "payload": "後台開起來了"}]


def test_notifier_keeps_no_telegram_token_of_its_own(paths, fake_notifier):
    """金鑰只存在 Notifier_TB 那一份 config.json，這個專案的 .env 一個都不用。"""
    write_notifier_config(paths, port=fake_notifier.port)
    write(paths.root / ".env", "LINE_CHANNEL_ACCESS_TOKEN=tok\n")
    assert notify.Notifier(paths).send("不用 token 也送得出去") is True


def test_notifier_can_be_pointed_somewhere_else(paths, fake_notifier):
    home = write_notifier_config(paths, port=fake_notifier.port, folder="SomewhereElse")
    write(paths.root / ".env", f"NOTIFIER_TB_HOME={home}\n")
    assert notify.Notifier(paths).send("換個地方") is True
    fake_notifier.wait(1)
    assert fake_notifier.payloads == ["換個地方"]


def test_notifier_can_be_switched_off(paths, fake_notifier):
    write_notifier_config(paths, port=fake_notifier.port)
    write(paths.root / ".env", "NOTIFIER_DISABLED=1\n")
    notifier = notify.Notifier(paths)
    assert not notifier.enabled and notifier.send("不該送出去") is False
    assert fake_notifier.received == []


def test_notifier_never_raises_when_notifier_tb_is_down(paths):
    """通知送不出去絕對不能變成發送失敗——這是整個模組最重要的一條。"""
    write_notifier_config(paths, port=dead_port())
    assert notify.Notifier(paths).send("會失敗的一則") is False


def test_messages_are_kept_and_resent_when_notifier_comes_back(paths):
    """Notifier_TB 是「要用才開」的，沒開著的時候訊息不能就這樣不見。"""
    write_notifier_config(paths, port=dead_port())
    notifier = notify.Notifier(paths)
    assert notifier.send("後台當掉了") is False
    assert notifier.send("後台又起來了") is False
    assert notifier.pending() == 2

    server = FakeNotifier()
    try:  # Notifier 開起來了
        write_notifier_config(paths, port=server.port)
        assert notifier.flush() == 2
        assert notifier.pending() == 0
        server.wait(2)
        assert [p.splitlines()[-1] for p in server.payloads] == ["後台當掉了", "後台又起來了"]
        assert all(p.startswith("（補送・") for p in server.payloads)
    finally:
        server.close()


def test_a_successful_send_also_drains_the_backlog(paths, fake_notifier):
    write_notifier_config(paths, port=dead_port())
    notifier = notify.Notifier(paths)
    notifier.send("之前沒送出去的")

    write_notifier_config(paths, port=fake_notifier.port)
    assert notifier.send("現在這一則") is True
    fake_notifier.wait(2)
    assert fake_notifier.payloads[0] == "現在這一則"  # 當下那則先走，補送的跟在後面
    assert "之前沒送出去的" in fake_notifier.payloads[1]
    assert notifier.pending() == 0


def test_the_backlog_does_not_grow_for_ever(paths, monkeypatch):
    # 直接讓 _post 回 False：這裡要測的是「存起來」那段的上限，不是連線本身
    # （真的連 200 次沒人聽的埠，光是被拒絕就要等好幾分鐘）
    write_notifier_config(paths, port=dead_port())
    monkeypatch.setattr(notify.Notifier, "_post", staticmethod(lambda target, text: False))
    notifier = notify.Notifier(paths)
    for i in range(notify.OUTBOX_LIMIT + 20):
        notifier.send(f"第 {i} 則")
    assert notifier.pending() == notify.OUTBOX_LIMIT  # 只留最後那些，最舊的丟掉
    kept = notifier.outbox.read_text(encoding="utf-8")
    assert "第 19 則" not in kept and "第 20 則" in kept  # 丟掉的是最舊的那幾則


# --------------------------------------------------------------------------- 上一次有沒有正常關閉


def test_clean_shutdown_is_remembered(paths):
    history = History(paths.db_file)
    assert notify.record_start(history) == {}  # 第一次開，沒有上一次
    notify.record_stop(history, clean=True, reason="正常關閉")
    assert notify.previous_shutdown_line(notify.record_start(history)) == ""


def test_a_crash_is_noticed_next_time_it_starts(paths):
    """今天那一種：後台自己不見了，record_stop 根本沒跑到。下次開起來要講出來。"""
    history = History(paths.db_file)
    notify.record_start(history)  # 開了…然後被強制關掉，沒有 record_stop
    line = notify.previous_shutdown_line(notify.record_start(history))
    assert "上一次沒有正常關閉" in line


# --------------------------------------------------------------------------- 各牧區狀況


def test_status_collects_roster_last_send_and_next_send(ministry):
    church = Church(ministry.church)
    now = dt.datetime(2026, 9, 11, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    [state] = status_mod.collect(church, now=now)
    assert state.name == "測試牧區"
    assert "排到 2026/12/27" in state.roster
    assert state.last_text == "（還沒發過）"
    assert state.next_run is not None and state.next_run.strftime("%m/%d %H:%M") == "09/17 20:00"
    assert state.schedule == "每星期四 20:00"
    assert state.ok


def test_status_flags_a_roster_that_is_running_out(paths):
    write(paths.config_dir / "roster.csv", "日期,講員\n2026/9/13,王牧師\n")
    settings = Settings()
    settings.source.kind, settings.source.csv_path = "csv", "roster.csv"
    save_settings(paths, settings)
    now = dt.datetime(2026, 9, 11, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    [state] = status_mod.collect(Church(paths.church), now=now)
    assert not state.ok and "剩 2 天" in state.roster


def test_status_says_so_when_auto_send_is_off(ministry):
    settings = Settings()
    settings.source.kind, settings.source.csv_path = "csv", "roster.csv"
    settings.schedule.enabled = False
    save_settings(ministry, settings)
    [state] = status_mod.collect(Church(ministry.church))
    assert state.next_run is None and state.next_text == "（不會自動發）"


def test_status_command_prints_and_can_push_to_telegram(ministry, monkeypatch, capsys, fake_notifier):
    write_notifier_config(ministry, port=fake_notifier.port)
    monkeypatch.setenv("CHURCH_BOT_HOME", str(ministry.root))
    assert main(["狀態", "--telegram"]) == 0
    printed = capsys.readouterr().out
    assert "測試牧區" in printed and "下一次發送" in printed and "已傳到 Telegram" in printed
    fake_notifier.wait(1)
    assert "測試牧區" in fake_notifier.payloads[0]


def test_status_command_exit_code_is_1_when_something_needs_attention(paths, monkeypatch, capsys):
    write(paths.config_dir / "roster.csv", "日期,講員\n2020/1/5,王牧師\n")  # 早就排完了
    settings = Settings()
    settings.source.kind, settings.source.csv_path = "csv", "roster.csv"
    save_settings(paths, settings)
    monkeypatch.setenv("CHURCH_BOT_HOME", str(paths.root))
    assert main(["狀態"]) == 1
    assert "已經排完了" in capsys.readouterr().out


# --------------------------------------------------------------------------- 設定頁

SETTINGS_FORM = {"source_kind": "csv", "csv_path": "config/roster.demo.csv", "day_of_week": "sat", "time": "20:00",
                 "timezone": "Asia/Taipei", "lookahead_days": "7", "roster_low_warning_days": "14",
                 "every_n_weeks": "1", "messenger_kind": "console"}


def test_settings_page_has_the_report_section(client):
    page = client.get("/settings").text
    assert "發完之後的彙報" in page and 'name="report_on_success"' in page
    assert 'name="report_target_id"' not in page  # 主責同工就是管理員，不另外設一組


def test_settings_page_saves_the_report_switches(client, paths):
    from church_bot.config import load_settings

    client.post("/settings", data={**SETTINGS_FORM, "report_on_success": "on"})
    saved = load_settings(paths).notify
    assert saved.report_on_success
    assert not saved.telegram  # checkbox 沒勾就是關

    client.post("/settings", data={**SETTINGS_FORM, "report_telegram": "on"})
    saved = load_settings(paths).notify
    assert saved.telegram and not saved.report_on_success
