"""伺服器管理員的 Telegram 播報（core/notify.py）與「各牧區狀況」（status.py、cli.bat 狀態）。"""

from __future__ import annotations

import datetime as dt

import httpx
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


# --------------------------------------------------------------------------- Notifier


class FakeTelegram:
    """擋住 httpx.post，記下送出去的東西。回傳值照 Telegram Bot API 的樣子。"""

    def __init__(self, ok: bool = True, boom: Exception | None = None) -> None:
        self.ok, self.boom, self.calls = ok, boom, []

    def __call__(self, url, *, timeout=None, json=None):
        self.calls.append((url, json))
        if self.boom is not None:
            raise self.boom
        return httpx.Response(200, json={"ok": self.ok, "description": "" if self.ok else "chat not found"})


def test_notifier_is_quiet_until_telegram_is_set_up(paths, monkeypatch):
    fake = FakeTelegram()
    monkeypatch.setattr(httpx, "post", fake)
    notifier = notify.Notifier(paths)
    assert not notifier.enabled
    assert notifier.send("哈囉") is False
    assert fake.calls == []  # 沒設定就完全不連網，不是連了才失敗


def test_notifier_sends_to_the_server_manager(paths, monkeypatch):
    write(paths.root / ".env", "TELEGRAM_BOT_TOKEN=tok123\nSERVER_MANAGER_TELEGRAM_ID=4242\n")
    fake = FakeTelegram()
    monkeypatch.setattr(httpx, "post", fake)
    notifier = notify.Notifier(paths)
    assert notifier.enabled and notifier.send("後台開起來了") is True
    url, body = fake.calls[0]
    assert url == "https://api.telegram.org/bottok123/sendMessage"
    assert body["chat_id"] == "4242" and body["text"] == "後台開起來了"
    assert "parse_mode" not in body  # 純文字：牧區名稱裡有 < & * 都不會把訊息弄壞


@pytest.mark.parametrize("fake", [FakeTelegram(ok=False),
                                  FakeTelegram(boom=httpx.ConnectError("沒網路"))])
def test_notifier_never_raises(paths, monkeypatch, fake):
    """通知傳不出去絕對不能變成發送失敗——這是整個模組最重要的一條。"""
    write(paths.root / ".env", "TELEGRAM_BOT_TOKEN=tok\nSERVER_MANAGER_TELEGRAM_ID=1\n")
    monkeypatch.setattr(httpx, "post", fake)
    assert notify.Notifier(paths).send("會失敗的一則") is False


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


def test_status_command_prints_and_can_push_to_telegram(ministry, monkeypatch, capsys):
    write(ministry.root / ".env", "TELEGRAM_BOT_TOKEN=tok\nSERVER_MANAGER_TELEGRAM_ID=9\n")
    monkeypatch.setenv("CHURCH_BOT_HOME", str(ministry.root))
    fake = FakeTelegram()
    monkeypatch.setattr(httpx, "post", fake)
    assert main(["狀態", "--telegram"]) == 0
    printed = capsys.readouterr().out
    assert "測試牧區" in printed and "下一次發送" in printed and "已傳到 Telegram" in printed
    assert "測試牧區" in fake.calls[0][1]["text"]


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
