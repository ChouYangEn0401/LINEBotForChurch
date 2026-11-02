"""現有指令加 --json：``quota --json``、``ministries list --json``（ClawBot 用來問用量、問「哪個牧區」）。"""

from __future__ import annotations

import pytest

from church_bot.church import add_ministry
from church_bot.config import load_settings, save_settings
from church_bot.messengers.base import Quota
from tests.cli_setup import fix_now, run, setup_ministry
from tests.conftest import FakeMessenger


@pytest.fixture
def fake(monkeypatch) -> FakeMessenger:
    messenger = FakeMessenger()
    messenger.quota_value = Quota(limit=200, used=20)
    monkeypatch.setattr("church_bot.service.build_messenger", lambda settings, paths: messenger)
    return messenger


@pytest.fixture
def line(paths, monkeypatch, fake):
    setup_ministry(paths, console=False)
    fix_now(monkeypatch)
    return paths


def test_quota_json(line, fake, capsys):
    code, out = run(line.church, "quota", "--force", capsys=capsys)
    assert code == 0, out
    assert out["enabled"] is True and out["used"] == 20 and out["limit"] == 200 and out["remaining"] == 180
    assert out["month"] and out["error"] == "" and "20 / 200" in out["describe"]


def test_quota_json_in_test_mode(paths, monkeypatch, capsys):
    setup_ministry(paths)
    code, out = run(paths.church, "quota", capsys=capsys)
    assert code == 0 and out == {"ok": True, "enabled": False, "message": out["message"]}


def test_ministries_list_json(paths, monkeypatch, capsys):
    setup_ministry(paths)
    settings = load_settings(paths)
    settings.schedule.enabled, settings.schedule.day_of_week, settings.schedule.time = True, "sat", "09:30"
    save_settings(paths, settings)
    add_ministry(paths.church, "壯年牧區")
    for argv in (("ministries", "list"), ("ministries",), ("牧區",)):
        code, out = run(paths.church, *argv, capsys=capsys)
        assert code == 0
        assert [m["id"] for m in out] == ["m1", "m2"]
        first = out[0]
        assert first["name"] == "測試牧區" and first["enabled"] is True and first["day_of_week"] == "sat"
        assert first["time"] == "09:30" and first["next_send"].startswith("20") and first["groups"] == 2
