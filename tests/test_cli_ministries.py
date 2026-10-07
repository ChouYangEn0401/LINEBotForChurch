"""指令列與多牧區：send 不指定牧區 = 今天輪到的牧區；--牧區 只處理那一個；「牧區」指令。"""

import argparse
import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from church_bot.church import add_ministry, load_church
from church_bot.cli import build_parser, cmd_ministries, cmd_send
from church_bot.config import Settings, save_settings
from church_bot.service import BotService

THURSDAY = dt.datetime(2026, 10, 8, 20, 0, tzinfo=ZoneInfo("Asia/Taipei"))


@pytest.fixture
def two(root, monkeypatch):
    for name, day, enabled in (("青年牧區", "thu", True), ("壯年牧區", "sat", True), ("約書亞牧區", "thu", False)):
        mpaths = root.for_ministry(add_ministry(root, name).id)
        settings = Settings()
        settings.messenger.kind = "console"
        settings.schedule.enabled, settings.schedule.day_of_week = enabled, day
        save_settings(mpaths, settings)
    ran: list[str] = []

    def fake_run(self, trigger, **kw):
        ran.append(self.paths.ministry)
        from church_bot.models import RunReport
        return RunReport(run_id="x", trigger=trigger, dry_run=False, started_at=THURSDAY), None

    monkeypatch.setattr(BotService, "run", fake_run)
    monkeypatch.setattr(BotService, "now", lambda self, settings: THURSDAY)
    return ran


def args(*argv):
    return build_parser().parse_args(["send", *argv])


def test_send_without_a_ministry_sends_the_ones_due_today(root, two, capsys):
    assert cmd_send(root, args()) == 0
    assert two == ["m1"]  # 星期四、自動發送開著的只有青年牧區；約書亞牧區關著
    out = capsys.readouterr().out
    assert "「壯年牧區」今天不是發送日" in out and "「約書亞牧區」今天不是發送日" in out


def test_send_to_one_ministry_by_name_or_all(root, two):
    assert cmd_send(root, args("--牧區", "壯年牧區")) == 0
    assert cmd_send(root, args("-m", "m3")) == 0
    assert two == ["m2", "m3"]  # 指定了就發，不管是不是發送日
    two.clear()
    cmd_send(root, args("--all"))
    assert two == ["m1", "m2", "m3"]


def test_unknown_ministry_lists_the_real_ones(root, two):
    from church_bot.errors import ConfigError

    with pytest.raises(ConfigError) as exc:
        cmd_send(root, args("--牧區", "不存在"))
    assert "青年牧區" in exc.value.hint


def test_ministries_command(root, two, capsys):
    parser = build_parser()
    cmd_ministries(root, parser.parse_args(["牧區", "rename", "m2", "約拿單牧區"]))
    cmd_ministries(root, parser.parse_args(["牧區", "add", "兒童牧區"]))
    assert [m.name for m in load_church(root).ministries] == ["青年牧區", "約拿單牧區", "約書亞牧區", "兒童牧區"]
    cmd_ministries(root, parser.parse_args(["牧區"]))
    out = capsys.readouterr().out
    assert "m4\t兒童牧區" in out and "每星期四 20:00" in out
