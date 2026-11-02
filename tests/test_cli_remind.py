"""``remind now``（＝ send --force 只對這個牧區）：試算、真的送、只送一個群組、失敗的群組、測試模式。"""

from __future__ import annotations

import pytest

from church_bot.church import add_ministry
from church_bot.messengers.base import Quota
from church_bot.ministries import Church
from tests.cli_setup import fix_now, run, setup_ministry
from tests.conftest import FakeMessenger, gid


@pytest.fixture
def fake(monkeypatch) -> FakeMessenger:
    messenger = FakeMessenger()
    messenger.sizes = {gid(): 30, gid("c"): 5}
    messenger.quota_value = Quota(limit=200, used=20)
    monkeypatch.setattr("church_bot.service.build_messenger", lambda settings, paths: messenger)
    return messenger


@pytest.fixture
def line(paths, monkeypatch, fake):
    """正式發送（LINE 換成假的）。"""
    setup_ministry(paths, console=False)
    fix_now(monkeypatch)
    return paths


def test_remind_now_dry_run_sends_nothing_and_counts_the_cost(line, fake, capsys):
    code, out = run(line.church, "remind", "now", "--dry-run", "--by=telegram:42", capsys=capsys)
    assert code == 0, out
    assert out["dry_run"] is True and out["sent"] == 0 and out["planned"] == 2
    assert fake.sent == []
    assert {g["name"]: g["audience"] for g in out["groups"]} == {"同工群": 30, "司琴群": 5}
    assert out["messages_used"] == 35 and out["messages_exact"] is True
    assert out["quota"]["limit"] == 200 and out["quota"]["used"] == 20
    assert "35" in out["message"]
    assert Church(line.church).service("m1").history.recent_runs() == []  # 試算不記錄


def test_remind_now_sends_even_if_already_sent(line, fake, capsys):
    code, out = run(line.church, "remind", "now", "--by=telegram:42", capsys=capsys)
    assert code == 0, out
    assert out["sent"] == 2 and out["failed"] == 0 and all(g["ok"] for g in out["groups"])
    assert {to for to, _m in fake.sent} >= {gid(), gid("c")}
    code, again = run(line.church, "remind", "now", "--by=telegram:42", capsys=capsys)
    assert code == 0 and again["sent"] == 2  # 已經送過也照送（--force）
    runs = Church(line.church).service("m1").history.recent_runs()
    assert [r.trigger for r in runs] == ["cli", "cli"]
    report = Church(line.church).service("m1").history.get_report(runs[0].id)
    assert any("telegram:42" in i["message"] for i in report["issues"])


def test_remind_now_one_target(line, fake, capsys):
    code, out = run(line.church, "remind", "now", "--target=司琴群", "--by=telegram:42", capsys=capsys)
    assert code == 0, out
    assert [g["name"] for g in out["groups"]] == ["司琴群"] and {to for to, _m in fake.sent} <= {gid("c"), *fake.sizes}
    assert all(to != gid() for to, _m in fake.sent)
    code, out = run(line.church, "remind", "now", "--target=不存在的群", "--by=telegram:42", capsys=capsys)
    assert code == 1 and set(out["groups"]) == {"同工群", "司琴群"}


def test_remind_now_reports_a_failed_group(line, fake, capsys):
    from church_bot.errors import MessengerError

    fake.fail[gid()] = MessengerError("LINE 拒絕這個操作", "確認機器人還在群組裡", status_code=403)
    code, out = run(line.church, "remind", "now", "--by=telegram:42", capsys=capsys)
    assert code == 1 and out["ok"] is False
    by_name = {g["name"]: g for g in out["groups"]}
    assert by_name["同工群"]["ok"] is False and "拒絕" in by_name["同工群"]["error"]
    assert by_name["司琴群"]["ok"] is True and out["sent"] == 1 and out["failed"] == 1


def test_remind_now_needs_by(line, capsys):
    code, out = run(line.church, "remind", "now", "--dry-run", capsys=capsys)
    assert code == 1 and "--by" in out["message"]


def test_remind_now_in_test_mode(paths, monkeypatch, capsys):
    setup_ministry(paths)  # 測試模式（console）
    fix_now(monkeypatch)
    code, out = run(paths.church, "remind", "now", "--by=telegram:42", capsys=capsys)
    assert code == 0, out
    assert out["test_mode"] is True and out["sent"] == 2 and out["messages_used"] == 0 and out["quota"] is None
    assert (paths.data_dir / "outbox.log").exists()


def test_remind_now_with_two_ministries_needs_ministry(line, capsys):
    add_ministry(line.church, "壯年牧區")
    code, out = run(line.church, "remind", "now", "--by=telegram:42", capsys=capsys)
    assert code == 1 and [m["id"] for m in out["ministries"]] == ["m1", "m2"]
