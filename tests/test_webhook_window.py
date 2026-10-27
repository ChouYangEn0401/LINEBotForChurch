"""LINE 指令的臨時網址開多久、開關時通知誰（擁有者 2026-10-15，見 webhook_window.py）。

* 開幾小時自己關（預設 3），關之前 10 分鐘預告；nssm 重開它時間照舊；``webhook on`` 可以延長。
* 開發者預設經 Notifier_TB 收到；也可以換成「執行一個程式」或關掉（開源出去不用綁 Telegram）。
* 牧區管理員的 LINE 私訊預設關（額度不夠）；開了的話「請聯絡○○」是勾了「伺服器管理員」的人。
* 只有伺服器管理員能勾「伺服器管理員」。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import threading
import time

import pytest

from church_bot import cli, runctl, tray
from church_bot import webhook_window as ww
from church_bot.church import add_ministry, edit_ministry, hash_password, load_church, update_church
from church_bot.config import WebhookSettings, load_webhook_settings
from church_bot.models import Member
from church_bot.tables import MemberTable
from tests.conftest import CHURCH, FakeMessenger, uid

T0 = dt.datetime(2026, 10, 15, 15, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))


def set_webhook(paths, **values) -> None:
    update_church(paths.church, lambda cfg: cfg.webhook.update(values))


def write_clawbot(paths, *, token: str, extra: dict | None = None, url: str = "http://127.0.0.1:8765") -> None:
    """假裝旁邊有一份 ClawBot：它的 data/gateway.json（網址＋ token）。"""
    from church_bot.core import notify

    folder = paths.church.root.parent / notify.CLAWBOT_DIR_NAME / "data"
    folder.mkdir(parents=True, exist_ok=True)
    tokens = {token: {"user_id": 1, "name": "owner"}}
    tokens.update(extra or {})
    (folder / "gateway.json").write_text(json.dumps({"url": url, "tokens": tokens}, ensure_ascii=False),
                                          encoding="utf-8")


# --------------------------------------------------------------------------- 設定


def test_defaults_are_three_hours_ten_minutes_notifier_and_no_line(root):
    s = load_webhook_settings(root)
    assert (s.hours, s.warn_minutes, s.dev_notice, s.dev_command) == (3, 10, "notifier_tb", [])
    assert not s.line_admins_on_open and not s.line_admins_on_close  # LINE 額度不夠：預設關


def test_settings_live_in_church_yaml_and_survive_other_edits(paths):
    set_webhook(paths, hours=2, line_admins_on_close=True)
    add_ministry(paths, "壯年牧區")  # 改牧區清單時不能把 webhook 那一段弄丟
    assert load_church(paths).webhook == {"hours": 2, "line_admins_on_close": True}
    s = load_webhook_settings(paths.for_ministry("m2"))  # 從哪個牧區讀都是整個教會那一份
    assert s.hours == 2 and s.line_admins_on_close


def test_a_broken_webhook_section_falls_back_to_defaults(paths):
    set_webhook(paths, hours="很久", dev_notice="pigeon")
    assert load_webhook_settings(paths) == WebhookSettings()


# --------------------------------------------------------------------------- 開到幾點


def test_opening_writes_the_deadline_and_a_restart_keeps_it(root):
    first = ww.begin(root, WebhookSettings(), now=T0)
    assert first.until == T0 + dt.timedelta(hours=3) and first.is_open
    again = ww.begin(root, WebhookSettings(hours=1), now=T0 + dt.timedelta(minutes=30))  # nssm 重開
    assert again.until == first.until


def test_a_fresh_request_sets_the_hours_and_a_stale_one_does_not(root):
    ww.request(root, 1.5, "telegram", now=T0)
    opened = ww.begin(root, WebhookSettings(), now=T0 + dt.timedelta(seconds=40))
    assert opened.hours == 1.5 and opened.by == "telegram"
    assert opened.until == T0 + dt.timedelta(seconds=40) + dt.timedelta(hours=1.5)
    ww.finish(root, "測試", now=T0 + dt.timedelta(hours=1))
    ww.request(root, 5, "telegram", now=T0)
    late = ww.begin(root, WebhookSettings(), now=T0 + dt.timedelta(hours=2))  # 單子留了兩小時沒人開：不算
    assert late.hours == 3


def test_after_closing_the_next_opening_starts_a_new_window(root):
    ww.begin(root, WebhookSettings(), now=T0)
    closed = ww.finish(root, "時間到", now=T0 + dt.timedelta(hours=3))
    assert closed.reason == "時間到" and not closed.is_open
    assert ww.finish(root, "又一次") is None  # 本來就關了
    reopened = ww.begin(root, WebhookSettings(hours=2), now=T0 + dt.timedelta(hours=4))
    assert reopened.until == T0 + dt.timedelta(hours=6)


def test_extending_counts_from_now_and_zero_means_never(root):
    ww.begin(root, WebhookSettings(), now=T0)
    longer = ww.extend(root, 5, "telegram", now=T0 + dt.timedelta(hours=2))
    assert longer.until == T0 + dt.timedelta(hours=7) and not longer.warned
    never = ww.extend(root, 0, "telegram", now=T0 + dt.timedelta(hours=2))
    assert never.until is None and "不會自動關" in ww.describe(never)


def test_the_reason_given_before_closing_is_kept(root):
    ww.begin(root, WebhookSettings(), now=T0)
    ww.mark_closing(root, "telegram 關的")
    assert ww.finish(root, "服務停止或按了 Ctrl+C").reason == "telegram 關的"


# --------------------------------------------------------------------------- 看時間：預告、時間到


class FakeClock:
    def __init__(self, start: dt.datetime, step: dt.timedelta) -> None:
        self.now, self.step = start, step

    def __call__(self) -> dt.datetime:
        current = self.now
        self.now += self.step
        return current


def run_watch(paths, clock, **kwargs):
    seen: list[tuple[str, ww.Window, WebhookSettings]] = []
    closed: list[bool] = []

    def announce(p, event, window, *, settings=None, **rest):
        seen.append((event, window, settings))

    stop = threading.Event()
    worker = threading.Thread(target=ww.watch, args=(paths, lambda: closed.append(True), stop),
                              kwargs={"announce_fn": announce, "now_fn": clock, "check_every": 0.001, **kwargs})
    worker.start()
    worker.join(10)
    stop.set()
    assert not worker.is_alive()
    return seen, closed


def test_it_warns_ten_minutes_before_then_closes_once(paths):
    set_webhook(paths, line_admins_on_close=True)
    ww.begin(paths, WebhookSettings(hours=0.25), now=T0)  # 15 分鐘
    seen, closed = run_watch(paths, FakeClock(T0, dt.timedelta(minutes=1)))
    assert [event for event, *_ in seen] == [ww.CLOSING_SOON, ww.CLOSED] and closed == [True]
    warn_window, close_settings = seen[0][1], seen[1][2]
    assert warn_window.remaining(T0 + dt.timedelta(minutes=5)) == 600  # 剛好剩 10 分鐘那一刻預告
    assert not close_settings.line_admins_on_close  # 預告過了：關的那一刻不再傳 LINE 給牧區管理員
    final = ww.read(paths)
    assert not final.is_open and final.reason == ww.REASON_TIME


def test_without_a_warning_the_admins_hear_about_it_when_it_closes(paths):
    set_webhook(paths, warn_minutes=0, line_admins_on_close=True)
    ww.begin(paths, WebhookSettings(hours=0.1), now=T0)
    seen, closed = run_watch(paths, FakeClock(T0, dt.timedelta(minutes=2)))
    assert [event for event, *_ in seen] == [ww.CLOSED] and seen[0][2].line_admins_on_close and closed


def test_never_closing_and_an_outside_close_both_end_quietly(paths):
    ww.begin(paths, WebhookSettings(hours=0), now=T0)
    stop = threading.Event()
    worker = threading.Thread(target=ww.watch, args=(paths, lambda: None, stop),
                              kwargs={"announce_fn": lambda *a, **k: pytest.fail("不該通知"), "check_every": 0.01})
    worker.start()
    time.sleep(0.1)
    assert worker.is_alive()  # 不自動關：一直等
    ww.finish(paths, "telegram 關的")
    worker.join(5)
    assert not worker.is_alive()  # 別人記成關掉了：自己結束


# --------------------------------------------------------------------------- 通知的內容


def test_admin_message_says_access_stops_who_to_contact_and_reminders_continue():
    window = ww.Window(until=T0 + dt.timedelta(minutes=10), hours=3, opened_at=T0)
    text = ww.admin_text(ww.CLOSING_SOON, window, ["周牧師"], now=T0)
    assert "再 10 分鐘" in text and "後台將停止訪問" in text and "請聯絡伺服器管理員（周牧師）" in text
    assert "每週的服事提醒照常" in text
    assert "請聯絡伺服器管理員。" in ww.admin_text(ww.CLOSED, window, [])


def test_dev_message_has_the_url_deadline_and_how_to_extend():
    window = ww.Window(until=T0 + dt.timedelta(hours=3), hours=3, opened_at=T0)
    text = ww.dev_text(ww.OPENED, window, url="https://a-b.trycloudflare.com")
    assert "https://a-b.trycloudflare.com" in text and "3 小時" in text and "webhook on --hours" in text
    assert "已關閉（時間到）" in ww.dev_text(ww.CLOSED, window, reason="時間到")


def test_server_admins_come_from_every_ministry_once(paths):
    add_ministry(paths, "壯年牧區")
    MemberTable(paths.members_file).save([Member("周牧師", server_admin=True), Member("小明")])
    MemberTable(paths.for_ministry("m2").members_file).save([Member("周牧師", server_admin=True),
                                                             Member("阿德", server_admin=True)])
    from church_bot.ministries import Church

    assert ww.server_admin_names(Church(paths.church)) == ["周牧師", "阿德"]


# --------------------------------------------------------------------------- 通知的方式


def test_the_developer_hears_through_notifier_tb_by_default(paths, monkeypatch):
    from church_bot.core import notify

    sent: list[str] = []
    monkeypatch.setattr(notify.Notifier, "send", lambda self, text, **kw: sent.append(text) or True)
    monkeypatch.setattr(ww, "tell_admins", lambda *a, **k: pytest.fail("預設不傳 LINE"))
    ww.announce(paths, ww.CLOSING_SOON, ww.begin(paths, WebhookSettings(), now=T0), now=T0)
    assert len(sent) == 1 and "自動關" in sent[0]


def test_a_command_gets_the_event_as_arguments_and_json(paths, tmp_path):
    out = tmp_path / "event.json"
    script = ("import json, os, sys; json.dump({'argv': sys.argv[1:], 'env': json.loads(os.environ['CHURCH_BOT_EVENT_JSON'])},"
              f" open(r'{out}', 'w', encoding='utf-8'), ensure_ascii=False)")
    set_webhook(paths, dev_notice="command", dev_command=[sys.executable, "-c", script, "{event}", "{until}", "{text}"])
    window = ww.begin(paths, WebhookSettings(), now=T0)
    ww.announce(paths, ww.OPENED, window, url="https://x.trycloudflare.com", now=T0)
    got = json.loads(out.read_text(encoding="utf-8"))
    assert got["argv"][:2] == ["opened", "2026-10-15T18:00:00+08:00"] and "x.trycloudflare.com" in got["argv"][2]
    assert got["env"]["event"] == "opened" and got["env"]["url"] == "https://x.trycloudflare.com"
    assert got["env"]["source"] == "church-bot" and got["env"]["hours"] == 3


def test_clawbot_tells_everyone_who_may_open_the_window(paths, monkeypatch):
    """dev_notice: clawbot → 開好／延長／快關／已關送給「能開這條網址的人」＋擁有者，不經 Notifier_TB。"""
    from church_bot.core import notify

    posted: list[tuple[str, dict]] = []
    monkeypatch.setattr(notify.ClawBotLink, "_post", lambda self, path, body: posted.append((path, body)) or
                        {"sent": [111], "failed": [], "owner": True})
    monkeypatch.setattr(notify.ClawBotLink, "is_owner", lambda self: True)
    monkeypatch.setattr(notify.Notifier, "send", lambda *a, **k: pytest.fail("有 ClawBot 就不該退回 Notifier_TB"))
    write_clawbot(paths, token="t-owner")
    set_webhook(paths, dev_notice="clawbot")
    window = ww.begin(paths, WebhookSettings(), now=T0)
    for event in ww.SHARED_EVENTS:
        ww.announce(paths, event, window, url="https://x.trycloudflare.com", now=T0)
    assert [p for p, _ in posted] == ["/v1/service/broadcast"] * 4
    assert {b["grant"] for _p, b in posted} == {"line_bot"} and {b["source"] for _p, b in posted} == {"LINE"}
    assert "x.trycloudflare.com" in posted[0][1]["text"] and "自動關" in posted[2][1]["text"]


def test_the_back_office_s_own_troubles_stay_with_the_server_admin(paths, monkeypatch):
    """網址斷了、停不掉服務：講的是服務、nssm、cloudflared，只給伺服器管理員（擁有者 2026-10-27）。"""
    from church_bot.core import notify

    monkeypatch.setattr(notify.ClawBotLink, "_post",
                        lambda self, path, body: pytest.fail("後台自己的事不該送給有權限的人"))
    monkeypatch.setattr(notify.ClawBotLink, "is_owner", lambda self: True)
    sent: list[str] = []
    monkeypatch.setattr(notify.Notifier, "send", lambda self, text, **kw: sent.append(text) or True)
    write_clawbot(paths, token="t-owner")
    set_webhook(paths, dev_notice="clawbot")
    window = ww.begin(paths, WebhookSettings(), now=T0)
    for event in ("restarting", "stop_failed"):
        ww.tell_dev(paths, load_webhook_settings(paths), ww.event_payload(event, f"⚠️ {event}", window))
    assert len(sent) == 2 and all(e in "".join(sent) for e in ("restarting", "stop_failed"))
    assert "restarting" not in ww.SHARED_EVENTS and "stop_failed" not in ww.SHARED_EVENTS


def test_when_clawbot_is_not_running_the_notice_falls_back_to_notifier_tb(paths, monkeypatch):
    """Host 沒開就退回舊路——這一條在，改成 clawbot 也不會掉通知。"""
    from church_bot.core import notify

    monkeypatch.setattr(notify.ClawBotLink, "_post", lambda self, path, body: None)   # 連不上
    monkeypatch.setattr(notify.ClawBotLink, "is_owner", lambda self: True)
    sent: list[str] = []
    monkeypatch.setattr(notify.Notifier, "send", lambda self, text, **kw: sent.append(text) or True)
    write_clawbot(paths, token="t-owner")
    set_webhook(paths, dev_notice="clawbot")
    ww.announce(paths, ww.OPENED, ww.begin(paths, WebhookSettings(), now=T0), now=T0)
    assert len(sent) == 1 and "開好了" in sent[0]


def test_clawbot_is_only_looked_for_where_it_actually_is(paths, monkeypatch):
    """沒有旁邊那個資料夾、或 .env 關掉了：find_clawbot 回 None，照舊走 Notifier_TB。"""
    from church_bot.core import notify

    assert notify.find_clawbot(paths) is None            # 還沒寫 gateway.json
    write_clawbot(paths, token="t-owner")
    monkeypatch.setattr(notify.ClawBotLink, "is_owner", lambda self: True)
    link = notify.find_clawbot(paths)
    assert link is not None and link.url == "http://127.0.0.1:8765"
    monkeypatch.setenv(notify.CLAWBOT_DISABLE_ENV, "1")
    assert notify.find_clawbot(paths) is None            # .env／環境變數關掉


def test_a_token_that_is_not_the_machine_owner_is_not_used(paths, monkeypatch):
    """gateway.json 裡可能留著別的、名字也叫 owner 的舊 token：用 /v1/me 問 ClawBot 認哪一個。"""
    from church_bot.core import notify

    write_clawbot(paths, token="t-stale", extra={"t-owner": {"user_id": 999, "name": "owner"}})
    monkeypatch.setattr(notify.ClawBotLink, "is_owner", lambda self: self.token == "t-owner")
    link = notify.find_clawbot(paths)
    assert link is not None and link.token == "t-owner"
    monkeypatch.setattr(notify.ClawBotLink, "is_owner", lambda self: False)
    assert notify.find_clawbot(paths) is None            # 一個都不是擁有者的


def test_off_means_nobody_is_told(paths, monkeypatch):
    from church_bot.core import notify

    set_webhook(paths, dev_notice="off")
    monkeypatch.setattr(notify.Notifier, "send", lambda *a, **k: pytest.fail("關掉了還在送"))
    ww.announce(paths, ww.CLOSED, ww.begin(paths, WebhookSettings(), now=T0))


def test_line_admins_get_one_message_each_only_when_switched_on(paths, monkeypatch):
    from church_bot import messengers
    from church_bot.core import notify

    monkeypatch.setattr(notify.Notifier, "send", lambda *a, **k: True)
    fake = FakeMessenger()
    monkeypatch.setattr(messengers, "build_messenger", lambda settings, p: fake)
    add_ministry(paths, "壯年牧區")
    MemberTable(paths.members_file).save([Member("周牧師", line_user_id=uid("1"), admin=True, server_admin=True),
                                          Member("小明", line_user_id=uid("2"))])
    MemberTable(paths.for_ministry("m2").members_file).save([Member("周牧師", line_user_id=uid("1"), admin=True),
                                                             Member("阿德", line_user_id=uid("3"), admin=True)])
    window = ww.begin(paths, WebhookSettings(), now=T0)
    ww.announce(paths, ww.OPENED, window)
    ww.announce(paths, ww.CLOSING_SOON, window)
    assert fake.sent == []  # 兩個開關預設都關
    set_webhook(paths, line_admins_on_close=True)
    ww.announce(paths, ww.CLOSING_SOON, window, now=T0)
    assert sorted(to for to, _ in fake.sent) == [uid("1"), uid("3")]  # 同一個人只傳一次、不是管理員的不傳
    assert "請聯絡伺服器管理員（周牧師）" in fake.sent[0][1].text


# --------------------------------------------------------------------------- 伺服器管理員（同工名單）


def test_server_admin_column_round_trips_and_old_files_still_load(paths):
    table = MemberTable(paths.members_file)
    paths.members_file.write_text("名字,LINE_userId,管理員\n小明,,是\n", encoding="utf-8-sig")
    assert table.load().items == [Member("小明", admin=True)]
    table.save([Member("周牧師", server_admin=True)])
    assert "伺服器管理員" in paths.members_file.read_text(encoding="utf-8-sig").splitlines()[0]
    assert table.load().items[0].server_admin


def test_only_the_server_manager_can_mark_a_server_admin(client, paths):
    assert client.post("/members/server-admin", data={"name": "陳小明", "enabled": "1"}).status_code == 200
    assert next(m for m in MemberTable(paths.members_file).load().items if m.name == "陳小明").server_admin
    assert "伺服器管理員" in client.get("/members").text

    from church_bot.config import update_env_file

    edit_ministry(paths.church, "m1", password_hash=hash_password("abcd"))
    update_env_file(paths.env_file, {"SERVER_MANAGER_PASSWORD": "manager-password-123"})
    client.cookies.clear()
    client.post(CHURCH + "/m/m1/unlock", data={"password": "abcd"})  # 牧區管理員
    assert client.post("/members/server-admin", data={"name": "陳小明", "enabled": "0"}).status_code == 403
    page = client.get("/members?edit=陳小明").text
    assert 'name="server_admin"' not in page and "/members/server-admin" not in page
    # 牧區管理員編輯這個人（表單裡沒有那一格）：原本勾的不能被洗掉
    client.post("/members/save", data={"name": "陳小明", "original_name": "陳小明", "active": "on", "note": "改備註"})
    saved = next(m for m in MemberTable(paths.members_file).load().items if m.name == "陳小明")
    assert saved.note == "改備註" and saved.server_admin
    client.post("/members/save", data={"name": "新來的", "active": "on", "server_admin": "on"})  # 自己加也不行
    assert not next(m for m in MemberTable(paths.members_file).load().items if m.name == "新來的").server_admin


# --------------------------------------------------------------------------- cli.bat webhook on / off / status


def webhook_args(action="status", **kw):
    return argparse.Namespace(action=action, hours=kw.get("hours"), wait=kw.get("wait", 0), by=kw.get("by", "telegram"),
                              json=kw.get("json", True))


class FakeService:
    """假的 Windows 服務：記下被叫了什麼，不碰真的服務。"""

    def __init__(self, state="stopped") -> None:
        self.state, self.calls = state, []

    def install(self, monkeypatch, paths):
        monkeypatch.setattr(runctl, "service_state", lambda program: self.state if program is runctl.WEBHOOK else "running")
        monkeypatch.setattr(runctl, "service_app_alive", lambda program: self.state == "running")
        monkeypatch.setattr(tray, "_scm_control", self.control)
        self.paths = paths

    def control(self, service, action, wait_seconds=60.0):
        self.calls.append((service, action))
        if action == "start":
            self.state = "running"
            runctl.mark_running(self.paths, runctl.WEBHOOK, detail="https://new-tunnel.trycloudflare.com/line/webhook")
        else:
            self.state = "stopped"
            runctl.run_file(self.paths, runctl.WEBHOOK).unlink(missing_ok=True)
        return True, "ok", 0


def last_json(capsys) -> dict:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_status_when_nothing_is_open(paths, monkeypatch, capsys):
    FakeService().install(monkeypatch, paths)
    assert cli.cmd_webhook(paths, webhook_args()) == 0
    got = last_json(capsys)
    assert got["running"] is False and got["summary"] == "沒開" and got["hours_default"] == 3


def test_on_starts_the_service_with_the_requested_hours(paths, monkeypatch, capsys):
    service = FakeService()
    service.install(monkeypatch, paths)
    assert cli.cmd_webhook(paths, webhook_args("on", hours=2, wait=5)) == 0
    got = last_json(capsys)
    assert service.calls == [("church-bot-webhook", "start")] and got["ok"] and got["message"] == "開好了"
    assert got["url"] == "https://new-tunnel.trycloudflare.com"
    request = ww.read(paths)
    assert request.is_request and request.hours == 2 and request.by == "telegram"  # 服務開起來時照這張單子


def test_on_while_open_extends_and_tells_the_developer(paths, monkeypatch, capsys):
    service = FakeService("running")
    service.install(monkeypatch, paths)
    runctl.mark_running(paths, runctl.WEBHOOK, detail="https://up.trycloudflare.com/line/webhook")
    ww.begin(paths, WebhookSettings())
    told: list[str] = []
    monkeypatch.setattr(ww, "announce", lambda p, event, window, **kw: told.append(event))
    assert cli.cmd_webhook(paths, webhook_args("on", hours=1)) == 0
    got = last_json(capsys)
    assert service.calls == [] and told == [ww.EXTENDED] and 55 <= got["remaining_minutes"] <= 60
    assert got["mode"] == "service" and got["url"] == "https://up.trycloudflare.com"


def test_off_stops_the_service_and_says_who_closed_it(paths, monkeypatch, capsys):
    service = FakeService("running")
    service.install(monkeypatch, paths)
    runctl.mark_running(paths, runctl.WEBHOOK, detail="https://up.trycloudflare.com/line/webhook")
    ww.begin(paths, WebhookSettings())
    assert cli.cmd_webhook(paths, webhook_args("off")) == 0
    assert service.calls == [("church-bot-webhook", "stop")] and last_json(capsys)["running"] is False
    assert ww.read(paths).reason == "telegram 關的"  # 開著的那個程式收工時用這一句
    assert cli.cmd_webhook(paths, webhook_args("off")) == 0 and last_json(capsys)["message"] == "本來就沒開"


def test_on_refuses_silly_hours(paths, monkeypatch, capsys):
    FakeService().install(monkeypatch, paths)
    assert cli.cmd_webhook(paths, webhook_args("on", hours=100)) == 1
    assert "0（不自動關）到 72" in last_json(capsys)["message"]


def test_a_permission_problem_is_explained(paths, monkeypatch, capsys):
    FakeService().install(monkeypatch, paths)
    monkeypatch.setattr(tray, "_scm_control", lambda *a, **k: (False, "沒有權限", tray.ERROR_ACCESS_DENIED))
    assert cli.cmd_webhook(paths, webhook_args("on")) == 1
    assert "install.bat" in last_json(capsys)["message"]


# --------------------------------------------------------------------------- 開著的那個程式（cli.cmd_tunnel）


def _tunnel_args(tmp_path):
    return argparse.Namespace(port=8787, cloudflared=str(tmp_path / "cloudflared.exe"))


def fake_cloudflared(monkeypatch):
    import church_bot.tunnel as tunnel_mod

    def fake_run_tunnel(command, register, out, started=None):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        started(proc)
        return proc.wait()

    monkeypatch.setattr(tunnel_mod, "run_tunnel", fake_run_tunnel)
    monkeypatch.setattr(cli, "_service_running", lambda name=cli.SERVICE_NAME: False)


def test_a_double_clicked_tunnel_closes_itself_when_time_is_up(paths, monkeypatch, tmp_path):
    fake_cloudflared(monkeypatch)
    set_webhook(paths, hours=0.0005, warn_minutes=0)  # 不到 2 秒
    told: list[tuple[str, str]] = []
    monkeypatch.setattr(ww, "announce", lambda p, event, window, **kw: told.append((event, kw.get("reason", ""))))
    started = time.monotonic()
    assert cli.cmd_tunnel(paths, _tunnel_args(tmp_path)) == runctl.ENDED_FROM_TRAY  # 3-open-webhook 不停下來等按鍵
    assert time.monotonic() - started < 15
    assert told == [(ww.CLOSED, ww.REASON_TIME)]  # 只通知一次（收工那一段不會再講一次）
    assert ww.read(paths).reason == ww.REASON_TIME and runctl.running(paths, runctl.WEBHOOK) is None


def test_under_the_service_time_up_stops_the_service_instead_of_exiting(paths, monkeypatch, tmp_path):
    import church_bot.tunnel as tunnel_mod

    monkeypatch.setenv("CHURCH_BOT_SERVICE", "1")
    monkeypatch.setattr(runctl, "ignore_logoff_when_service", lambda: None)
    monkeypatch.setattr(runctl, "service_state", lambda program: "stop_pending")
    set_webhook(paths, hours=0.0005, warn_minutes=0)
    told: list[str] = []
    monkeypatch.setattr(ww, "announce", lambda p, event, window, **kw: told.append(event))
    holder: dict = {}

    def fake_run_tunnel(command, register, out, started=None):
        holder["proc"] = proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        started(proc)
        return proc.wait()

    stopped: list[str] = []

    def scm(service, action, wait_seconds=60.0):  # 真的 Windows：停服務 = nssm 送 Ctrl+C，程式收工
        stopped.append(f"{service} {action}")
        holder["proc"].terminate()
        return True, "已停止", 0

    monkeypatch.setattr(tunnel_mod, "run_tunnel", fake_run_tunnel)
    monkeypatch.setattr(tray, "_scm_control", scm)
    code = cli.cmd_tunnel(paths, _tunnel_args(tmp_path))
    assert stopped == ["church-bot-webhook stop"] and code != runctl.ENDED_FROM_TRAY
    assert told == [ww.CLOSED] and ww.read(paths).reason == ww.REASON_TIME


def test_a_crash_under_the_service_keeps_the_window_and_warns_the_developer(paths, monkeypatch):
    monkeypatch.setattr(runctl, "service_state", lambda program: "running")  # nssm 等一下會把它開回來
    told: list[str] = []
    monkeypatch.setattr(ww, "tell_dev", lambda p, s, payload, **kw: told.append(payload["event"]))
    window = ww.begin(paths, WebhookSettings(), now=T0)
    cli._tunnel_closed(paths, {}, 1, under_service=True)
    assert told == ["restarting"] and ww.read(paths).until == window.until and ww.read(paths).is_open


def test_stopping_the_service_records_and_announces_the_close(paths, monkeypatch):
    monkeypatch.setattr(runctl, "service_state", lambda program: "stop_pending")
    told: list[tuple[str, str]] = []
    monkeypatch.setattr(ww, "announce", lambda p, event, window, **kw: told.append((event, kw["reason"])))
    ww.begin(paths, WebhookSettings(), now=T0)
    ww.mark_closing(paths, "telegram 關的")
    cli._tunnel_closed(paths, {}, 0, under_service=True)
    assert told == [(ww.CLOSED, "telegram 關的")] and not ww.read(paths).is_open


# --------------------------------------------------------------------------- 小圖示、安裝


def test_the_tray_says_the_webhook_is_on_demand():
    stopped = tray.describe(runctl.WEBHOOK, "stopped", None)
    assert "要用時按下面的「▶ 開 webhook」" in tray.service_line(stopped, runctl.WEBHOOK)
    # 擁有者 2026-10-15：「小格子按鈕的視窗裡面我也沒有看到啟動 webhook」——選單直接寫開／關 webhook 和開幾小時
    assert tray.service_label(runctl.WEBHOOK, "start") == "▶ 開 webhook（3 小時後自己關；服務：啟動）"
    assert tray.service_label(runctl.WEBHOOK, "stop").startswith("⏹ 關掉 webhook")
    assert tray.service_label(runctl.WEB, "start") == "服務：啟動（nssm start）"
    assert "開機會自己起來" in tray.service_line(tray.describe(runctl.WEB, "stopped", None), runctl.WEB)
    assert tray.menu_state(stopped)["start"]  # 手動啟動的服務停著：照樣按得了「服務：啟動」


def test_the_tray_explains_a_timed_close(paths):
    ww.begin(paths, WebhookSettings())
    ww.finish(paths, ww.REASON_TIME)
    icon = tray.Tray.__new__(tray.Tray)
    icon.paths, icon.program = paths.church, runctl.WEBHOOK
    icon.status = tray.describe(runctl.WEBHOOK, "stopped", None)
    assert icon._down_text().startswith("關了（時間到）")


def test_installer_makes_only_the_webhook_manual_start():
    from pathlib import Path

    script = (Path(__file__).resolve().parents[1] / "scripts" / "windows" / "service" / "service.ps1").read_text(
        encoding="utf-8-sig")
    assert "@('Start', $(if ($key -eq 'web') { 'SERVICE_DELAYED_AUTO_START' } else { 'SERVICE_DEMAND_START' }))" in script
