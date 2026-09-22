"""不用一直開著視窗的用法：排程判斷（send --scheduled）、跨程式的鎖、Windows 工作排程器、自動登記 Webhook。"""

import argparse
import datetime as dt
import json
import sys
import threading
import time
from zoneinfo import ZoneInfo

import httpx
import pytest

from church_bot import wintask
from church_bot.cli import cmd_send, cmd_set_webhook
from church_bot.config import Settings, save_settings
from church_bot.errors import MessengerError
from church_bot.locking import process_lock
from church_bot.messengers.line import LineMessenger
from church_bot.models import RunReport, Target
from church_bot.scheduler import scheduled_skip_reason
from church_bot.service import BotService
from church_bot.tables import TargetTable
from tests.conftest import FakeMessenger, gid, write

TZ = ZoneInfo("Asia/Taipei")
THU_2005 = dt.datetime(2026, 10, 1, 20, 5, tzinfo=TZ)  # 週四 20:05，預設排程（週四 20:00）剛過


@pytest.fixture
def fake(monkeypatch) -> FakeMessenger:
    messenger = FakeMessenger()
    monkeypatch.setattr("church_bot.service.build_messenger", lambda settings, paths: messenger)
    return messenger


@pytest.fixture
def service(paths, fake) -> BotService:
    write(paths.config_dir / "roster.csv", "日期,講員,司琴\n2026/9/13,王牧師,小明\n2026/9/20,李傳道,美華\n")
    settings = Settings()
    settings.source.kind = "csv"
    settings.source.csv_path = "config/roster.csv"
    save_settings(paths, settings)
    TargetTable(paths.targets_file).save([Target("同工群", gid()), Target("敬拜團", gid("c"))])
    svc = BotService(paths)
    svc.now = lambda settings: dt.datetime(2026, 9, 11, 20, 0, tzinfo=TZ)
    return svc


def ran_at(service: BotService, when: dt.datetime, trigger: str = "schedule") -> None:
    service.history.record(RunReport(run_id=f"r-{when:%H%M%S}", trigger=trigger, dry_run=False, started_at=when,
                                     finished_at=when))


# ------------------------------------------------------------------ send --scheduled 的判斷


def test_scheduled_run_is_due_only_right_after_the_scheduled_time(service):
    assert scheduled_skip_reason(service, THU_2005) is None
    assert "還沒到發送時間" in scheduled_skip_reason(service, THU_2005 - dt.timedelta(days=2))
    # 超過 12 小時就不補發了（跟 2-start 的開機補發一樣）
    assert "還沒到發送時間" in scheduled_skip_reason(service, THU_2005 + dt.timedelta(hours=13))
    # 電腦睡著錯過，幾個小時後醒來還是會補發
    assert scheduled_skip_reason(service, THU_2005 + dt.timedelta(hours=3)) is None


def test_scheduled_run_happens_once_per_fire_time(service):
    ran_at(service, THU_2005 - dt.timedelta(minutes=4))  # 20:01 已經跑過
    assert "已經發過了" in scheduled_skip_reason(service, THU_2005)
    ran_at(service, THU_2005 - dt.timedelta(days=7), trigger="reply")  # /提醒 不算「排程跑過」
    assert scheduled_skip_reason(service, THU_2005 + dt.timedelta(days=7)) is None


def test_failed_scheduled_run_is_retried_a_few_times(service):
    """電腦剛睡醒、網路還沒連上就去讀服事表會失敗：下一次檢查（15 分鐘後）再試，但最多 3 次，免得一直通知管理員。"""
    from church_bot.models import Issue, Severity

    def failed_at(when):
        report = RunReport(run_id=f"f-{when:%H%M%S}", trigger="schedule", dry_run=False, started_at=when,
                           finished_at=when)
        report.issues.append(Issue(Severity.ERROR, "SourceError", "讀不到服事表"))
        service.history.record(report)

    failed_at(THU_2005 - dt.timedelta(minutes=5))
    assert scheduled_skip_reason(service, THU_2005 + dt.timedelta(minutes=10)) is None  # 再試一次
    failed_at(THU_2005 + dt.timedelta(minutes=10))
    failed_at(THU_2005 + dt.timedelta(minutes=25))
    assert "試了 3 次都失敗" in scheduled_skip_reason(service, THU_2005 + dt.timedelta(minutes=40))


def test_scheduled_run_respects_the_auto_send_switch(service, paths):
    settings = Settings.model_validate({"source": {"kind": "csv", "csv_path": "config/roster.csv"},
                                        "schedule": {"enabled": False}})
    save_settings(paths, settings)
    assert "自動發送已關閉" in scheduled_skip_reason(service, THU_2005)


def test_send_scheduled_does_nothing_when_not_due(service, paths, fake, monkeypatch, capsys):
    monkeypatch.setattr("church_bot.scheduler.scheduled_skip_reason", lambda svc: "還沒到發送時間")
    assert cmd_send(paths, argparse.Namespace(scheduled=True, force=False)) == 0
    assert "這次不發" in capsys.readouterr().out
    assert fake.sent == [] and BotService(paths).history.recent_runs() == []


def test_send_scheduled_sends_and_is_labelled_as_the_schedule(service, paths, fake, monkeypatch):
    monkeypatch.setattr("church_bot.scheduler.scheduled_skip_reason", lambda svc: None)
    monkeypatch.setattr(BotService, "now", lambda self, settings: dt.datetime(2026, 9, 11, 20, 0, tzinfo=TZ))
    cmd_send(paths, argparse.Namespace(scheduled=True, force=False))
    assert {to for to, _ in fake.sent} == {gid(), gid("c")}
    assert [r.trigger for r in BotService(paths).history.recent_runs()] == ["schedule"]


# ------------------------------------------------------------------ 跨程式的鎖


def test_two_programs_sending_at_the_same_moment_only_send_once(service, paths, fake):
    """2-start 的排程和 Windows 工作排程器剛好同一秒發送：第二個要等第一個記錄完，再查就會略過。"""
    original = fake.send

    def slow_send(to, message):
        time.sleep(0.3)
        return original(to, message)

    fake.send = slow_send
    other = BotService(paths)
    other.now = service.now
    threads = [threading.Thread(target=svc.run, args=("schedule",)) for svc in (service, other)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(to for to, _ in fake.sent) == sorted([gid(), gid("c")])


def test_lock_gives_up_waiting_instead_of_skipping_the_reminder(tmp_path):
    lock = tmp_path / "send.lock"
    with process_lock(lock) as first:
        with process_lock(lock, timeout=0.3) as second:
            assert first is True and second is False
    with process_lock(lock) as again:
        assert again is True


# ------------------------------------------------------------------ 自動登記 Webhook（免費模式）


def test_line_webhook_endpoints():
    seen: list[tuple[str, str, dict]] = []

    def handler(request):
        body = json.loads(request.content) if request.content else {}
        seen.append((request.method, request.url.path, body))
        if request.url.path.endswith("/test"):
            return httpx.Response(200, json={"success": True, "statusCode": 200, "reason": "OK", "detail": "200"})
        if request.method == "GET":
            return httpx.Response(200, json={"endpoint": "https://x/line/webhook", "active": True})
        return httpx.Response(200, json={})

    client = httpx.Client(base_url="https://api.line.me", transport=httpx.MockTransport(handler))
    line = LineMessenger("tok", client=client, sleep=lambda _: None)
    line.set_webhook("https://x/line/webhook")
    assert line.webhook_info() == ("https://x/line/webhook", True)
    assert line.test_webhook() == (True, "200 200")
    assert seen[0] == ("PUT", "/v2/bot/channel/webhook/endpoint", {"endpoint": "https://x/line/webhook"})


class FakeWebhookLine:
    calls: list = []
    active = True
    results = [(False, "502 COULD_NOT_CONNECT"), (True, "200 OK")]

    def __init__(self, *args, **kwargs):
        self._results = list(FakeWebhookLine.results)

    def set_webhook(self, url):
        FakeWebhookLine.calls.append(url)

    def webhook_info(self):
        return FakeWebhookLine.calls[-1], FakeWebhookLine.active

    def test_webhook(self):
        return self._results.pop(0)

    def close(self):
        pass


@pytest.fixture(autouse=True)
def tunnel_is_reachable(monkeypatch):
    monkeypatch.setattr("church_bot.tunnel.is_reachable", lambda url: True)


def test_set_webhook_adds_path_and_retries_until_line_can_connect(paths, monkeypatch, capsys):
    save_settings(paths, Settings())
    monkeypatch.setattr("church_bot.messengers.line.LineMessenger", FakeWebhookLine)
    FakeWebhookLine.calls, FakeWebhookLine.active = [], True
    args = argparse.Namespace(url="https://abc.trycloudflare.com/", tries=3, wait=0)
    assert cmd_set_webhook(paths, args) == 0
    assert FakeWebhookLine.calls == ["https://abc.trycloudflare.com/line/webhook"]
    assert "LINE 連得到機器人了" in capsys.readouterr().out


def test_set_webhook_warns_when_use_webhook_is_off(paths, monkeypatch, capsys):
    save_settings(paths, Settings())
    monkeypatch.setattr("church_bot.messengers.line.LineMessenger", FakeWebhookLine)
    FakeWebhookLine.calls, FakeWebhookLine.active = [], False
    assert cmd_set_webhook(paths, argparse.Namespace(url="https://abc.trycloudflare.com", tries=3, wait=0)) == 1
    assert "Use webhook" in capsys.readouterr().out


class NotFoundYetLine(FakeWebhookLine):
    """剛建好的臨時網址：LINE 前兩次登記都說「Invalid webhook endpoint URL」（還找不到）。"""

    rejections = 0

    def set_webhook(self, url):
        if NotFoundYetLine.rejections < 2:
            NotFoundYetLine.rejections += 1
            raise MessengerError("LINE 不接受這則訊息：Invalid webhook endpoint URL", status_code=400)
        super().set_webhook(url)

    def test_webhook(self):
        return True, "200 OK"


def test_register_waits_for_a_brand_new_tunnel_url(paths, monkeypatch):
    from church_bot.tunnel import register_webhook

    save_settings(paths, Settings())
    monkeypatch.setattr("church_bot.messengers.line.LineMessenger", NotFoundYetLine)
    FakeWebhookLine.calls, FakeWebhookLine.active, NotFoundYetLine.rejections = [], True, 0
    probes = iter([False, False, True])  # 前兩次從外面還連不到
    waits: list[float] = []
    printed: list[str] = []
    ok = register_webhook(paths, "https://new.trycloudflare.com", printed.append, wait=2,
                          reachable=lambda url: next(probes), sleep=waits.append)
    assert ok and FakeWebhookLine.calls == ["https://new.trycloudflare.com/line/webhook"]
    assert waits == [2, 2, 2, 2]  # 等網址生效 2 次 + LINE 拒絕 2 次
    assert any("等它生效" in line for line in printed)


def test_register_gives_up_with_a_clear_message(paths, monkeypatch):
    from church_bot.tunnel import register_webhook

    save_settings(paths, Settings())
    monkeypatch.setattr("church_bot.messengers.line.LineMessenger", NotFoundYetLine)
    FakeWebhookLine.calls, NotFoundYetLine.rejections = [], 0
    printed: list[str] = []
    assert not register_webhook(paths, "https://new.trycloudflare.com", printed.append, tries=2, wait=0,
                                sleep=lambda s: None)
    assert "登記到 LINE 失敗" in printed[-1]


def test_set_webhook_rejects_plain_http(paths, capsys):
    assert cmd_set_webhook(paths, argparse.Namespace(url="http://localhost:8787", tries=1, wait=0)) == 2


# ------------------------------------------------------------------ Windows 工作排程器


def test_task_command_is_valid_python_that_runs_send_scheduled(paths):
    exe, args = wintask.task_command(paths)
    assert exe.lower().endswith(("pythonw.exe", "python.exe", "python", "pythonw"))
    assert args.startswith('-c "') and args.endswith('"')
    code = args[4:-1]
    compile(code, "<task>", "exec")
    assert "['send', '--scheduled']" in code and repr(str(paths.root / "src")) in code


def test_install_script_quotes_for_powershell(paths):
    script = wintask.install_script(paths)
    assert f"-TaskName '{wintask.TASK_NAME}'" in script
    assert "''send'', ''--scheduled''" in script  # PowerShell 單引號字串裡的 ' 要寫兩次
    assert "-StartWhenAvailable" in script and "-AllowStartIfOnBatteries" in script
    assert "-Minutes 15" in script


def test_status_parsing(monkeypatch):
    monkeypatch.setattr(wintask, "_powershell", lambda script: "MISSING\r\n")
    assert wintask.status() is None
    monkeypatch.setattr(wintask, "_powershell", lambda script: "STATE=Ready\r\nLAST=1999/11/30 00:00\r\n"
                                                               "RESULT=267011\r\nNEXT=2026/09/22 21:15\r\n")
    never = wintask.status()
    assert never.state == "Ready" and never.last_run == "" and never.last_ok is None
    monkeypatch.setattr(wintask, "_powershell", lambda script: "STATE=Ready\r\nLAST=2026/09/22 21:00\r\n"
                                                               "RESULT=1\r\nNEXT=2026/09/22 21:15\r\n")
    assert wintask.status().last_ok is False
    monkeypatch.setattr(wintask, "_powershell", lambda script: "STATE=Running\r\nLAST=2026/09/22 21:00\r\n"
                                                               "RESULT=267009\r\nNEXT=2026/09/22 21:15\r\n")
    running = wintask.status()
    assert running.running and running.last_ok is None  # 正在跑不是錯誤


# ------------------------------------------------------------------ 免費模式：Python 自己開 cloudflared


FAKE_CLOUDFLARED = """
import sys, time
print("INF Thank you for trying Cloudflare Tunnel", flush=True)
print("INF |  https://quick-test-tunnel.trycloudflare.com  |", flush=True)
time.sleep(0.2)
print("INF Registered tunnel connection connIndex=0 protocol=quic", flush=True)
print("ERR something the admin should see", flush=True)
time.sleep(1.5)  # 真的 cloudflared 會一直開著；登記一定要在這段時間內完成，不能等到它結束
"""


def test_tunnel_registers_while_cloudflared_is_still_running(tmp_path):
    from church_bot.tunnel import run_tunnel

    script = tmp_path / "fake_cloudflared.py"
    script.write_text(FAKE_CLOUDFLARED, encoding="utf-8")
    registered: list[tuple[str, float]] = []
    printed: list[str] = []
    start = time.monotonic()
    code = run_tunnel([sys.executable, str(script)],
                      lambda url: registered.append((url, time.monotonic() - start)) or True, printed.append)
    total = time.monotonic() - start
    assert code == 0
    assert [url for url, _ in registered] == ["https://quick-test-tunnel.trycloudflare.com/line/webhook"]
    assert registered[0][1] < total - 1.0  # 在 cloudflared 還開著的時候就登記了
    assert any("免費模式開好了" in line for line in printed)
    assert any("something the admin should see" in line for line in printed)


def test_tunnel_explains_when_cloudflared_cannot_start(tmp_path):
    from church_bot.tunnel import run_tunnel

    printed: list[str] = []
    assert run_tunnel([str(tmp_path / "no-such-cloudflared.exe")], lambda url: True, printed.append) == 2
    assert "開不了 cloudflared" in printed[0]
