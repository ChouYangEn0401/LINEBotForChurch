"""給 Telegram 排程呼叫的發送（重試、失敗跳視窗）、跨程式的鎖、開機補發的判斷、免費模式（自動登記 Webhook）。"""

import argparse
import datetime as dt
import json
import sys
import threading
import time
from zoneinfo import ZoneInfo

import httpx
import pytest

from church_bot.cli import cmd_quota, cmd_send, cmd_set_webhook
from church_bot.config import Settings, save_settings
from church_bot.errors import MessengerError
from church_bot.locking import process_lock
from church_bot.messengers.line import LineMessenger
from church_bot.models import RunReport, Target
from church_bot.scheduler import scheduled_skip_reason
from church_bot.service import BotService
from church_bot.tables import TargetTable
from tests.conftest import FakeMessenger, gid, uid, write

TZ = ZoneInfo("Asia/Taipei")
THU_2005 = dt.datetime(2026, 10, 1, 20, 5, tzinfo=TZ)  # 週四 20:05，預設排程（週四 20:00）剛過
ADMIN = uid("f")


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
    settings.source.csv_path = "roster.csv"
    settings.line.admin_target_id = ADMIN
    save_settings(paths, settings)
    TargetTable(paths.targets_file).save([Target("同工群", gid()), Target("敬拜團", gid("c"))])
    svc = BotService(paths)
    svc.now = lambda settings: dt.datetime(2026, 9, 11, 20, 0, tzinfo=TZ)
    return svc


def ran_at(service: BotService, when: dt.datetime, trigger: str = "schedule") -> None:
    service.history.record(RunReport(run_id=f"r-{when:%H%M%S}", trigger=trigger, dry_run=False, started_at=when,
                                     finished_at=when))


# ------------------------------------------------------------------ 開機補發的判斷（2-start 內建排程）


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


def test_scheduled_run_respects_the_auto_send_switch(service, paths):
    settings = Settings.model_validate({"source": {"kind": "csv", "csv_path": "roster.csv"},
                                        "schedule": {"enabled": False}})
    save_settings(paths, settings)
    assert "自動發送已關閉" in scheduled_skip_reason(service, THU_2005)


# ------------------------------------------------------------------ Telegram 呼叫的 send：重試、失敗跳視窗


def send_args(**overrides):
    return argparse.Namespace(**{"force": False, "retries": 0, "retry_wait": 0, "popup": False, **overrides})


@pytest.fixture
def popups(monkeypatch):
    shown: list[tuple[str, str]] = []
    # cmd_send 自己建 BotService，「今天」要跟 service fixture 一樣固定住，服事表才不會過期
    monkeypatch.setattr(BotService, "now", lambda self, settings: dt.datetime(2026, 9, 11, 20, 0, tzinfo=TZ))
    monkeypatch.setattr("church_bot.cli._popup", lambda title, text: shown.append((title, text)))
    monkeypatch.setattr("church_bot.cli.time.sleep", lambda seconds: None)
    return shown


def flaky_roster(monkeypatch, failures: int) -> None:
    """前幾次讀服事表失敗（例如網路剛好斷掉），之後正常。"""
    from church_bot.errors import SourceError

    original = BotService.fetch_roster
    calls = {"n": 0}

    def fetch(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= failures:
            raise SourceError("連不上 Google Sheet", "檢查網路")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(BotService, "fetch_roster", fetch)


def test_send_retries_a_temporary_failure_then_succeeds(service, paths, fake, popups, monkeypatch, capsys):
    flaky_roster(monkeypatch, failures=1)
    assert cmd_send(paths, send_args(retries=3, popup=True)) == 0
    assert {to for to, _ in fake.sent if to != ADMIN} == {gid(), gid("c")}
    # 第一次失敗先不通知管理員（不浪費 LINE 則數）；成功那次只有平常的提醒事項（服事表快用完）
    assert all("連不上 Google Sheet" not in text for text in fake.texts_to(ADMIN))
    assert popups == []
    assert "第 1 次沒成功" in capsys.readouterr().out


def test_send_gives_up_after_retries_alerts_admin_once_and_pops_up(service, paths, fake, popups, monkeypatch):
    flaky_roster(monkeypatch, failures=99)
    assert cmd_send(paths, send_args(retries=2, popup=True)) == 1
    assert len(BotService(paths).history.recent_runs()) == 3  # 1 次 + 重試 2 次
    assert len(fake.texts_to(ADMIN)) == 1  # 只有最後一次才通知管理員
    assert len(popups) == 1 and "連不上 Google Sheet" in popups[0][1]


def test_send_does_not_retry_problems_that_waiting_cannot_fix(service, paths, fake, popups):
    TargetTable(paths.targets_file).save([Target("同工群", gid(), enabled=False)])  # 沒有啟用的群組
    assert cmd_send(paths, send_args(retries=3, popup=True)) == 1
    assert len(BotService(paths).history.recent_runs()) == 1
    assert len(popups) == 1


def test_send_without_popup_flag_never_pops_up(service, paths, fake, popups, monkeypatch):
    flaky_roster(monkeypatch, failures=99)
    assert cmd_send(paths, send_args()) == 1
    assert popups == []


def test_line_network_failure_counts_as_temporary(service, fake):
    from church_bot.errors import MessengerError
    from church_bot.service import worth_retrying

    fake.fail[gid()] = MessengerError("連不上 LINE", "檢查網路", retryable=True)
    report, _ = service.run("cli")
    assert worth_retrying(report)
    fake.fail[gid("c")] = MessengerError("機器人不在群組裡", "", status_code=403)
    fake.fail.pop(gid())
    report, _ = service.run("cli", force=True)
    assert not worth_retrying(report)


# ------------------------------------------------------------------ Telegram 呼叫的 quota：發完回頭確認用量


def test_quota_command_prints_and_stores_this_months_usage(service, paths, fake, capsys):
    from church_bot.messengers.base import Quota

    fake.quota_value = Quota(limit=200, used=40)
    assert cmd_quota(paths, argparse.Namespace(force=False)) == 0
    assert "本月已用 40 / 200 則" in capsys.readouterr().out
    assert service.quota_status().used == 40  # 存起來，管理網頁下次打開就是這個數字


def test_quota_command_after_a_send_picks_up_the_settled_number(service, paths, fake, capsys):
    """發送當下 LINE 的統計還沒算進這一次；Telegram 排在 5 分鐘後呼叫 quota，數字才對得上。"""
    from church_bot.messengers.base import Quota

    fake.quota_value = Quota(limit=200, used=40)
    service.run("cli")
    assert service.quota_status().pending  # 發完先標記「數字可能還沒算進這一次」

    fake.quota_value = Quota(limit=200, used=60)
    assert cmd_quota(paths, argparse.Namespace(force=True)) == 0
    after = service.quota_status()
    assert after.used == 60 and not after.pending
    assert "本月已用 60 / 200 則" in capsys.readouterr().out


def test_quota_command_reports_a_lookup_failure_but_keeps_the_old_number(service, paths, fake, monkeypatch, capsys):
    from church_bot.messengers.base import Quota

    fake.quota_value = Quota(limit=200, used=40)
    cmd_quota(paths, argparse.Namespace(force=False))
    def timeout():
        raise MessengerError("連 LINE 逾時", "檢查網路")

    monkeypatch.setattr(fake, "quota", timeout)
    assert cmd_quota(paths, argparse.Namespace(force=True)) == 1
    out = capsys.readouterr().out
    assert "本月已用 40 / 200 則" in out and "連 LINE 逾時" in out


# ------------------------------------------------------------------ 跨程式的鎖


def test_two_programs_sending_at_the_same_moment_only_send_once(service, paths, fake):
    """2-start 的排程和 Telegram 呼叫的 cli.bat 剛好同一秒發送：第二個要等第一個記錄完，再查就會略過。"""
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
    assert sorted(to for to, _ in fake.sent if to != ADMIN) == sorted([gid(), gid("c")])


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


def test_set_webhook_remembers_the_url_for_the_line_command(paths, monkeypatch):
    """登記的同時記下對外網址：管理員在 LINE 打「/服務網址」就拿得到，不用一個一個貼。"""
    save_settings(paths, Settings())
    monkeypatch.setattr("church_bot.messengers.line.LineMessenger", FakeWebhookLine)
    FakeWebhookLine.calls, FakeWebhookLine.active = [], True
    cmd_set_webhook(paths, argparse.Namespace(url="https://abc.trycloudflare.com/", tries=3, wait=0))
    current = BotService(paths).service_url()
    assert current.url == "https://abc.trycloudflare.com" and current.source == "register"


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
